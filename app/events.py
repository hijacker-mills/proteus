"""
Tool-event emission.

Events are the same shape the Hermes plugin used to write to a JSONL file:
    {ts, userId, tool, status, ms, ...context}

In acag they travel two ways:
  1. INLINE over the SSE stream (primary, scales per-connection — no shared file).
     The agent attaches each event to a chunk under the `qubi_tool_event` key.
  2. Optionally XADD'd to a Redis stream for cross-request analytics/audit,
     only when REDIS_URL is configured.
"""
from __future__ import annotations

import time
from typing import Any

from . import config
from .redisc import get_redis


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
