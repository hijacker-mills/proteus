"""
Web tools: search + fetch. Give the agent live information and the ability to
read pages — the "do you have a browser?" capability.

- web_search: Tavily (if TAVILY_API_KEY set, best quality) else DuckDuckGo HTML
  (no key required).
- web_fetch: r.jina.ai reader for clean markdown (no key) with a raw-strip fallback.
"""
from __future__ import annotations

import html
import re
from urllib.parse import unquote

from .. import config
from ..httpclient import get_client

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def _parse_ddg(page: str, count: int) -> list[dict]:
    results: list[dict] = []
    # anchors: <a ... class="result__a" href="...">title</a>
    anchors = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    for i, (href, title) in enumerate(anchors[:count]):
        url = href
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            url = unquote(m.group(1))
        elif href.startswith("//"):
            url = "https:" + href
        snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
        results.append({"title": _strip_html(title), "url": url, "snippet": snippet})
    return results


async def web_search(query: str, count: int = 5) -> dict:
    query = (query or "").strip()
    if not query:
        return {"error": "query required"}
    count = max(1, min(int(count or 5), 10))

    if config.TAVILY_API_KEY:
        try:
            r = await get_client().post(
                "https://api.tavily.com/search",
                json={"api_key": config.TAVILY_API_KEY, "query": query,
                      "max_results": count, "include_answer": True},
                timeout=20,
            )
            r.raise_for_status()
            d = r.json()
            return {
                "query": query,
                "answer": d.get("answer"),
                "results": [{"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content", "")[:300]}
                            for x in d.get("results", [])[:count]],
            }
        except Exception as exc:
            return {"error": f"tavily failed: {exc}"}

    try:
        r = await get_client().get(
            "https://html.duckduckgo.com/html/",
            params={"q": query}, headers={"User-Agent": _UA}, timeout=20,
        )
        r.raise_for_status()
        results = _parse_ddg(r.text, count)
        return {"query": query, "results": results} if results else {"query": query, "results": [], "note": "no results"}
    except Exception as exc:
        return {"error": f"search failed: {exc}"}


async def web_fetch(url: str, max_chars: int | None = None) -> dict:
    url = (url or "").strip()
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
