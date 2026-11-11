"""
Tool-event emission.

Events are the same shape the Hermes plugin used to write to a JSONL file:
    {ts, userId, tool, status, ms, ...context}

In proteus they travel two ways:
  1. INLINE over the SSE stream (primary, scales per-connection — no shared file).
     The agent attaches each event to a chunk under the `proteus_tool_event` key.
  2. Optionally XADD'd to a Redis stream for cross-request analytics/audit,
     only when REDIS_URL is configured.
"""
from __future__ import annotations

import time
from typing import Any

from . import config
from .redisc import get_redis


def _render_payload(tool: str, result: Any) -> dict[str, Any] | None:
    """A small, client-renderable slice of a tool result — allowlisted per tool.

    Only tools whose entire output is meant to be shown wholesale opt in, so the
    frontend can render a card directly from real data instead of depending on the
    model to re-emit it as text markers. Kept tiny (SSE size + privacy): tools whose
    useful output is the model's own prose (quiz_generate, recall_grade, memory_*)
    are intentionally NOT included. lecture_evidence sends only which lectures it
    found, never the passages.
    """
    if not isinstance(result, dict):
        return None
    if tool == "lecture_evidence":
        return _evidence_lectures(result)
    if tool == "image_search":
        imgs = result.get("images")
        if not isinstance(imgs, list):
            return None
        cards: list[dict[str, Any]] = []
        for im in imgs[:6]:
            if not isinstance(im, dict):
                continue
            src = im.get("src") or im.get("url")
            if not src:
                continue
            card: dict[str, Any] = {"src": str(src), "alt": str(im.get("alt") or "")[:200]}
            # Where the image came from. The scraper returns it and we used to
            # drop it, so diagrams reached the student with no attribution —
            # the one thing a study tool showing outside material owes them.
            source = im.get("source")
            if isinstance(source, dict) and source.get("label") and source.get("url"):
                card["source"] = {
                    "label": str(source["label"])[:80],
                    "url": str(source["url"])[:500],
                }
            cards.append(card)
        return {"images": cards} if cards else None
    if tool == "youtube_fetch":
        # `youtube_search` is not here on purpose: it returns candidates the
        # model still has to choose between, and shipping all of them would put
        # a shelf of unvetted videos on screen. Fetch is the commitment — the
        # agent is told to call it on the one it intends to recommend — so a
        # fetch event names exactly one video and is safe to draw.
        #
        # Chapters stay behind: the client shows one card, and which chapter to
        # open at is the model's judgement, carried in its own reply.
        vid = result.get("id")
        url = result.get("url")
        if not vid or not url:
            return None
        video = {"id": str(vid), "url": str(url)}
        for key, limit in (("title", 200), ("channel", 120), ("views", 40), ("thumbnail", 500)):
            value = result.get(key)
            if value:
                video[key] = str(value)[:limit]
        return {"video": video}
    if tool == "diagram":
        # A branded SVG the client draws natively (react-native-svg). The tool
        # returns svg=null when there was no structure worth drawing; nothing to
        # ship then. Capped for SSE size — diagrams are a few KB by design.
        svg = result.get("svg")
        if not isinstance(svg, str) or not svg.strip():
            return None
        payload: dict[str, Any] = {"svg": svg[:40000]}
        if result.get("alt"):
            payload["alt"] = str(result["alt"])[:200]
        if result.get("caption"):
            payload["caption"] = str(result["caption"])[:200]
        return payload
    return None


def _evidence_lectures(result: dict[str, Any]) -> dict[str, Any] | None:
    """The lectures a lecture_evidence search found, one entry each.

    The app shows these as chips under "Found 3 passages in ..." and checks
    them against the lecture the student has open. Titles and ids only: the
    excerpts are the student's notes and stay between the tool and the model.

    A passage whose lecture no longer resolves has no lectureId or
    lectureTitle, so it is listed under its note title instead. That keeps the
    list empty exactly when the search found nothing, which is what the app
    reads `items` for. `hits` is the passage count, as on the event itself.
    """
    raw = result.get("items")
    if not isinstance(raw, list):
        return None
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        lecture_id = item.get("lectureId")
        lecture_id = str(lecture_id) if lecture_id else None
        title = item.get("lectureTitle") or item.get("noteTitle")
        title = str(title).strip()[:120] if title else ""
        if not lecture_id and not title:
            continue
        key = f"id:{lecture_id}" if lecture_id else f"title:{title}"
        if key in seen:
            continue
        seen.add(key)
        entry: dict[str, str] = {"title": title}
        if lecture_id:
            entry["lectureId"] = lecture_id
        items.append(entry)
        if len(items) >= 10:
            break
    return {"items": items, "hits": len(raw)}


def make_event(
    user_id: str,
    tool: str,
    status: str,
    ms: float,
    args: dict[str, Any] | None = None,
    result: Any = None,
) -> dict[str, Any]:
    """Build a compact tool event with a little human-readable context."""
    ev: dict[str, Any] = {
        "ts": int(time.time() * 1000),
        "userId": user_id,
        "tool": tool,
        "status": status,
        "ms": round(ms),
    }
    args = args or {}
    if "query" in args:
        ev["query"] = str(args["query"])[:60]
    if "scope" in args:
        ev["scope"] = args["scope"]
    if "memory_type" in args:
        ev["type"] = args["memory_type"]
    if "text" in args:
        ev["chars"] = len(str(args["text"]))

    if isinstance(result, dict):
        if "error" in result:
            ev["error"] = str(result["error"])[:120]
        else:
            for key in ("items", "results", "memories", "evidence"):
                if isinstance(result.get(key), list):
                    ev["hits"] = len(result[key])
                    break

    # Structured render payload for tools with a frontend card (image_search,
    # youtube_fetch, the lectures lecture_evidence found). Rides along on the
    # same `proteus_tool_event` chunk; telemetry fields above are unchanged, so
    # existing consumers keep working.
    data: dict[str, Any] = {}
    if status == "ok":
        data.update(_render_payload(tool, result) or {})
    # The query also goes in `data`, on every status, because the app's
    # activity row reads the subject ("Searching your notes for ...") from
    # there. The top-level `query` stays for older readers.
    if "query" in ev:
        data["query"] = ev["query"]
    if data:
        ev["data"] = data
    return ev


async def push(event: dict[str, Any]) -> None:
    """Best-effort append to the Redis tool-event stream (no-op without REDIS_URL)."""
    client = await get_redis()
    if client is None:
        return
    try:
        flat = {k: str(v) for k, v in event.items()}
        await client.xadd(config.TOOL_EVENT_STREAM, flat, maxlen=100_000, approximate=True)
    except Exception:
        pass  # telemetry must never break a chat
