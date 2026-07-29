"""
YouTube search — keyless. Finds the best educational videos for a study topic
so Qubi can recommend one (or a few) to watch.

No Google API key needed: we call YouTube's own InnerTube endpoint (the same one
youtube.com's web client uses; its "key" is a public constant baked into the page,
not a Cloud API key). Falls back to scraping `ytInitialData` from the results HTML
if InnerTube changes shape. Returns a clean, render-ready shape:

    {"videos": [{id, title, channel, duration, views, published, url, thumbnail, description}]}
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..httpclient import get_client

# Public InnerTube web key — a constant embedded in youtube.com's client JS
# (NOT a per-project Google Cloud key). Stable for years; used by yt-dlp et al.
_INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"
_CLIENT_VERSION = "2.20240726.00.00"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _runs(node: dict[str, Any] | None) -> str:
    if not node:
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(r.get("text", "") for r in node.get("runs", []))


def _video_from_renderer(v: dict[str, Any]) -> dict[str, Any] | None:
    vid = v.get("videoId")
    if not vid:
        return None
    desc = _runs(v.get("descriptionSnippet")) or "".join(
        _runs(s) for s in v.get("detailedMetadataSnippets", [{}])[:1] for s in [s.get("snippetText", {})]
    )
    return {
        "id": vid,
        "title": _runs(v.get("title")),
        "channel": _runs(v.get("ownerText")) or _runs(v.get("longBylineText")),
        "duration": (v.get("lengthText") or {}).get("simpleText"),  # e.g. "6:02"; None for live
        "views": _runs(v.get("viewCountText")),
        "published": _runs(v.get("publishedTimeText")),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "description": (desc or "")[:200],
    }


def _walk_video_renderers(node: Any, out: list[dict], limit: int) -> None:
    """Depth-first collect videoRenderer nodes from any InnerTube/ytInitialData tree."""
    if len(out) >= limit:
        return
    if isinstance(node, dict):
        vr = node.get("videoRenderer")
        if isinstance(vr, dict):
            item = _video_from_renderer(vr)
            if item:
                out.append(item)
                if len(out) >= limit:
                    return
        for val in node.values():
            _walk_video_renderers(val, out, limit)
    elif isinstance(node, list):
        for val in node:
            _walk_video_renderers(val, out, limit)


async def _innertube(query: str, limit: int) -> list[dict]:
    body = {
        "context": {"client": {"clientName": "WEB", "clientVersion": _CLIENT_VERSION, "hl": "en", "gl": "US"}},
        "query": query,
        # sp = EgIQAQ%3D%3D → filter to "Video" results only (drops channels/playlists/shelves)
        "params": "EgIQAQ%3D%3D",
    }
    r = await get_client().post(
        f"https://www.youtube.com/youtubei/v1/search?key={_INNERTUBE_KEY}&prettyPrint=false",
        json=body,
        headers={"Content-Type": "application/json", "User-Agent": _UA},
        timeout=20,
    )
    r.raise_for_status()
    out: list[dict] = []
    _walk_video_renderers(r.json(), out, limit)
    return out


async def _scrape(query: str, limit: int) -> list[dict]:
    """Fallback: pull ytInitialData out of the results page HTML."""
    r = await get_client().get(
        "https://www.youtube.com/results",
        params={"search_query": query, "hl": "en", "gl": "US"},
        headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+cb"},
        timeout=20,
    )
    r.raise_for_status()
    m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});</script>", r.text) or re.search(
        r'ytInitialData"\]\s*=\s*(\{.*?\});', r.text
    )
    if not m:
        return []
    data = json.loads(m.group(1))
    out: list[dict] = []
    _walk_video_renderers(data, out, limit)
    return out


async def youtube_search(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query required"}
    count = max(1, min(int(args.get("count", 4) or 4), 8))

    videos: list[dict] = []
    errors: list[str] = []
    for fn in (_innertube, _scrape):
        try:
            videos = await fn(query, count)
            if videos:
                break
        except Exception as exc:  # try the next strategy
            errors.append(f"{fn.__name__}: {exc}")

    if not videos:
        return {"error": "youtube search returned no results", "detail": "; ".join(errors) or None}
    return {"query": query, "videos": videos}
