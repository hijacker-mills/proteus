"""
The agent core: a provider-neutral tool-use loop.

`run()` is an async generator of structured events:
    {"type": "text",  "text": "..."}      incremental assistant text
    {"type": "tool",  "event": {...}}      a completed tool call (for live UI / audit)
    {"type": "done"}                        run finished cleanly
    {"type": "error", "message": "..."}    fatal error

It talks only to `llm.astream()` in OpenAI-shaped messages, so it is agnostic to
which model/provider is configured. Fully stateless: the caller supplies the
full history every request, so any replica can serve any turn — that is what
makes horizontal scaling trivial.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

from . import config, events, llm, profiles


def _prepare(messages: list[dict[str, Any]], extra_system: str, system_prompt: str) -> list[dict[str, Any]]:
    """Build the OpenAI-format message list: our system prompt first, then turns.
    Any system messages already in the history are folded into the system prompt."""
    folded: list[str] = []
    convo: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content", "")
        if role == "system":
            if isinstance(content, str) and content.strip():
                folded.append(content.strip())
        elif role in ("user", "assistant", "tool"):
            convo.append(m)

    system = system_prompt + "\n\nCurrent time: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + "."
    for part in (*folded, extra_system):
        if part:
            system += "\n\n" + part
    return [{"role": "system", "content": system}, *convo]


async def run(
    user_id: str,
    messages: list[dict[str, Any]],
    extra_system: str = "",
    profile: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    system_prompt, tools, dispatch = profiles.resolve(profile)
    convo = _prepare(messages, extra_system, system_prompt)
    if not any(m["role"] == "user" for m in convo):
        yield {"type": "error", "message": "no user message provided"}
        return

    try:
        for _turn in range(config.MAX_TOOL_TURNS):
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []

            async for ev in llm.astream(
                model=config.MODEL,
                messages=convo,
                tools=tools,
                max_tokens=config.MAX_TOKENS,
                temperature=config.TEMPERATURE,
            ):
                if ev["type"] == "text":
                    text_parts.append(ev["text"])
                    yield {"type": "text", "text": ev["text"]}
                elif ev["type"] == "final":
                    tool_calls = ev["tool_calls"]

            if not tool_calls:
                yield {"type": "done"}
                return

            # Record the assistant's tool-call turn (OpenAI message shape).
            convo.append({
                "role": "assistant",
                "content": "".join(text_parts),
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])},
                    }
                    for c in tool_calls
                ],
            })

            for c in tool_calls:
                t0 = time.time()
                try:
                    out = await dispatch(c["name"], user_id, c["arguments"])
                    status = "error" if isinstance(out, dict) and "error" in out else "ok"
                except Exception as exc:  # a tool must never crash the run
                    out = {"error": str(exc)}
                    status = "error"
                ms = (time.time() - t0) * 1000

                ev = events.make_event(user_id, c["name"], status, ms, c["arguments"], out)
                yield {"type": "tool", "event": ev}
                await events.push(ev)

                convo.append({
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": json.dumps(out),
                })

        yield {"type": "text", "text": "\n\n_(reached tool-call limit — try narrowing the question.)_"}
        yield {"type": "done"}
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}


async def close() -> None:
    await llm.close()
