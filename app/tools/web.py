"""
Web tools: search + fetch. Give the agent live information and the ability to
read pages — the "do you have a browser?" capability.

- web_search: Tavily (if TAVILY_API_KEY set, best quality) else DuckDuckGo HTML
  (no key required).
- web_fetch: r.jina.ai reader for clean markdown (no key) with a raw-strip fallback.
"""
from __future__ import annotations

import logging

import html
import re
from urllib.parse import unquote

from .. import config
from ..httpclient import get_client
from .url_safety import is_safe_url_async, refusal

logger = logging.getLogger("proteus.tools.web")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


async def _tavily(query: str, count: int) -> dict | None:
    r = await get_client().post("https://api.tavily.com/search", timeout=20,
        json={"api_key": config.TAVILY_API_KEY, "query": query,
              "max_results": count, "include_answer": True})
    r.raise_for_status()
    d = r.json()
    return {"provider": "tavily", "query": query, "answer": d.get("answer"),
            "results": [{"title": x.get("title"), "url": x.get("url"),
                         "snippet": (x.get("content") or "")[:300]}
                        for x in d.get("results", [])[:count]]}


async def _brave(query: str, count: int) -> dict | None:
    r = await get_client().get("https://api.search.brave.com/res/v1/web/search", timeout=20,
        params={"q": query, "count": count},
        headers={"X-Subscription-Token": config.BRAVE_SEARCH_API_KEY, "Accept": "application/json"})
    r.raise_for_status()
    d = r.json()
    return {"provider": "brave", "query": query,
            "results": [{"title": x.get("title"), "url": x.get("url"),
                         "snippet": (x.get("description") or "")[:300]}
                        for x in (d.get("web", {}).get("results") or [])[:count]]}


async def _serper(query: str, count: int) -> dict | None:
    r = await get_client().post("https://google.serper.dev/search", timeout=20,
        json={"q": query, "num": count},
        headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"})
    r.raise_for_status()
    d = r.json()
    return {"provider": "serper", "query": query, "answer": (d.get("answerBox") or {}).get("answer"),
            "results": [{"title": x.get("title"), "url": x.get("link"),
                         "snippet": (x.get("snippet") or "")[:300]}
                        for x in (d.get("organic") or [])[:count]]}


# Search-result extraction, done in the page. Engine-specific selectors first,
# then a generic heuristic, because every engine wraps its result links in a
# click-tracking redirect and the visible URL lives in a separate element.
def _extract_js(n: int) -> str:
    """Result extraction, run in the page. Engine-specific selectors first, then a
    generic heuristic — every engine wraps result links in a click-tracker and
    renders the real URL in a separate element."""
    return r"""() => {
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const sets = [
    ['#b_results li.b_algo', 'h2', 'cite', '.b_caption p, .b_algoSlug'],
    ['#search div.g', 'h3', 'cite', '.VwiC3b'],
    ['.result', '.result__title', '.result__url', '.result__snippet'],
    ['article', 'h3, h2', 'cite, a[href]', 'p'],
  ];
  for (const [item, t, c, s] of sets) {
    const rows = [...document.querySelectorAll(item)].slice(0, __N__).map(li => ({
      title: clean((li.querySelector(t) || {}).innerText),
      cite: clean((li.querySelector(c) || {}).innerText),
      snippet: clean((li.querySelector(s) || {}).innerText),
      href: (li.querySelector('a[href^=http]') || {}).href || '',
    })).filter(r => r.title);
    if (rows.length) return rows;
  }
  return [];
}""".replace("__N__", str(n))


def _cite_to_url(cite: str, href: str) -> str:
    """Rebuild the real URL from the breadcrumb an engine renders under a result.

    Engines show `example.com › docs › page` and put a click-tracker in the href.
    The scheme has to come off before splitting, or `https://a.com › b` splits
    into ['https:', 'a.com', 'b'] and the first part has no dot to recognise.
    The tracker is kept as a fallback, since it does still redirect correctly.
    """
    text = (cite or "").strip()
    if not text:
        return href
    scheme = "https://"
    for prefix in ("https://", "http://"):
        if text.lower().startswith(prefix):
            scheme, text = prefix, text[len(prefix):]
            break
    parts = [p.strip() for p in text.replace("\u203a", "/").split("/") if p.strip()]
    if not parts or "." not in parts[0] or " " in parts[0]:
        return href
    return scheme + "/".join(parts)


async def _via_browser(query: str, count: int) -> dict:
    """Keyless path: drive the real browser rather than scraping with an HTTP client.

    Scraping search engines over plain HTTP is a losing game — the endpoints that
    used to work now answer a headless client with a challenge page. A real
    browser gets the real page, which is the whole reason the browser tool is
    packaged rather than optional.
    """
    from urllib.parse import quote_plus

    from . import browser as browser_tool

    url = f"{config.SEARCH_URL_TEMPLATE}{quote_plus(query)}"
    snap = await browser_tool.browser("eval", url=url, script=_extract_js(count))
    rows = snap.get("result") if isinstance(snap, dict) else None
    if snap.get("error"):
        return {"error": f"browser search failed: {snap['error']}"}
    if not rows:
        return {"provider": "browser", "query": query, "results": [],
                "note": "the results page returned nothing recognisable; "
                        "set TAVILY_API_KEY / BRAVE_SEARCH_API_KEY for a real search API"}
    return {"provider": "browser", "query": query,
            "results": [{"title": r["title"][:140],
                         "url": _cite_to_url(r.get("cite", ""), r.get("href", "")),
                         "snippet": r.get("snippet", "")[:300]} for r in rows[:count]]}


async def web_search(query: str, count: int = 5) -> dict:
    """Keyed provider if one is configured, else a real browser."""
    query = (query or "").strip()
    if not query:
        return {"error": "query required"}
    count = max(1, min(int(count or 5), 10))

    for key, provider in ((config.TAVILY_API_KEY, _tavily),
                          (config.BRAVE_SEARCH_API_KEY, _brave),
                          (config.SERPER_API_KEY, _serper)):
        if not key:
            continue
        try:
            out = await provider(query, count)
            if out and out.get("results"):
                return out
        except Exception as exc:
            # Fall through to the next provider rather than failing the turn.
            logger.warning("search provider failed: %s", exc)

    if config.TOOLS_BROWSER:
        return await _via_browser(query, count)
    return {"error": "no search backend available — set TAVILY_API_KEY, "
                     "BRAVE_SEARCH_API_KEY or SERPER_API_KEY, or enable TOOLS_BROWSER=true"}


async def web_fetch(url: str, max_chars: int | None = None) -> dict:
    url = (url or "").strip()
    if not await is_safe_url_async(url):
        return refusal(url)
    if not url.startswith("http"):
        return {"error": "valid http(s) url required"}
    limit = max_chars or config.WEB_FETCH_MAX_CHARS

    # Preferred: r.jina.ai returns clean markdown without a key.
    try:
        r = await get_client().get(f"https://r.jina.ai/{url}", headers={"User-Agent": _UA}, timeout=25)
        if r.status_code == 200 and r.text.strip():
            return {"url": url, "content": r.text[:limit]}
    except Exception:
        pass

    try:
        r = await get_client().get(url, headers={"User-Agent": _UA}, timeout=25, follow_redirects=True)
        r.raise_for_status()
        return {"url": url, "content": _strip_html(r.text)[:limit]}
    except Exception as exc:
        return {"error": f"fetch failed: {exc}"}
