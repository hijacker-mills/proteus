"""
Synthetic model backend, for load-testing the gateway itself.

`MODEL=mock/<profile>` streams generated tokens instead of calling a provider,
so a load test measures Proteus (FastAPI, the agent loop, SSE fan-out, the tool
dispatcher) rather than an upstream model's throughput or rate limit. Pointing a
stress test at a real provider mostly measures that provider; pointing it at a
local Ollama mostly measures your CPU.

Profiles tune the shape of the stream:

    mock/instant   0 tokens of delay      — pure gateway overhead
    mock/fast      120 tokens @  5ms      — a fast hosted model
    mock/slow      400 tokens @ 25ms      — a slow/reasoning model, long-lived SSE
    mock/tool      calls `web_search` once, then answers

Override any profile inline: `mock/fast?tokens=500&delay_ms=10`.

It emits the same normalized events as `llm.astream`, so nothing downstream can
tell the difference. NEVER point production at this — `/healthz` reports the
active model, so a mock in prod is visible there.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator
from urllib.parse import parse_qs

_PROFILES: dict[str, dict[str, Any]] = {
    "instant": {"tokens": 60,  "delay_ms": 0,  "tool": None},
    "fast":    {"tokens": 120, "delay_ms": 5,  "tool": None},
    "slow":    {"tokens": 400, "delay_ms": 25, "tool": None},
    "tool":    {"tokens": 80,  "delay_ms": 5,  "tool": "web_search"},
}

_LOREM = (
    "A gateway accepts a request on one side and speaks whatever the other side "
    "expects, which is why the same agent can answer over HTTP, Telegram and a "
    "scheduled job without knowing the difference between them. "
)
_WORDS = _LOREM.split()


def _settings(spec: str) -> dict[str, Any]:
    """Parse `<profile>` or `<profile>?tokens=…&delay_ms=…`."""
    name, _, query = spec.partition("?")
    cfg = dict(_PROFILES.get(name or "fast", _PROFILES["fast"]))
    for key, values in parse_qs(query).items():
        if key in ("tokens", "delay_ms") and values:
            try:
                cfg[key] = max(0, int(values[0]))
            except ValueError:
                pass
        elif key == "tool" and values:
            cfg["tool"] = values[0] or None
    return cfg


async def astream(
    spec: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
    reasoning_effort: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    cfg = _settings(spec)

    # One tool call on the first pass, then a normal answer on the second, so the
    # full two-trip agent loop is exercised (dispatch, result injection, re-stream).
    wants_tool = cfg["tool"] and tools and not any(m.get("role") == "tool" for m in messages)
    if wants_tool:
        available = {t.get("function", {}).get("name") for t in tools}
        if cfg["tool"] in available:
            yield {
                "type": "final",
                "tool_calls": [{"id": "call_mock_1", "name": cfg["tool"],
                                "arguments": {"query": "load test"}}],
                "finish_reason": "tool_calls",
            }
            return

    delay = cfg["delay_ms"] / 1000.0
    count = min(cfg["tokens"], max_tokens) if max_tokens else cfg["tokens"]
    for i in range(count):
        if delay:
            await asyncio.sleep(delay)
        yield {"type": "text", "text": _WORDS[i % len(_WORDS)] + " "}

    yield {"type": "final", "tool_calls": [], "finish_reason": "stop"}
