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

import asyncio
import json
import time
from datetime import datetime, timezone
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

    # ORDER MATTERS FOR PROMPT CACHING. Providers cache on a byte-identical
    # PREFIX, so everything stable has to come first and everything volatile
    # last. The clock used to sit directly after the persona, which changed the
    # prompt every minute and made all of it — persona, client system messages
    # and (for prefix-matching providers) the tool schemas — uncacheable on
    # every single turn, including each tool round-trip within a turn.
    system = system_prompt
    for part in folded:
        if part:
            system += "\n\n" + part
    if extra_system:
        system += "\n\n" + extra_system
    # Volatile tail: keep it last, and keep it small. llm.CLOCK_PREFIX is how
    # providers needing an explicit cache breakpoint find the stable/volatile
    # boundary again, so the two must stay in sync.
    system += llm.CLOCK_PREFIX + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") + "."
    return [{"role": "system", "content": system}, *convo]


async def run(
    user_id: str,
    messages: list[dict[str, Any]],
    extra_system: str = "",
    profile: str | None = None,
    host_tools: bool = False,
) -> AsyncGenerator[dict[str, Any], None]:
    """`host_tools` grants the caller shell/run_code/email/schedule. It defaults
    to False and must be an explicit decision by the entry point (see
    toolsets.HOST_TOOLS) — the profile alone never unlocks host access."""
    system_prompt, tools, dispatch = profiles.resolve(profile, host_tools=host_tools)
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

            # Tool calls run CONCURRENTLY. Providers are asked for parallel tool
            # calls, so a turn routinely carries several; running them in
            # sequence made a turn cost the SUM of its tools instead of the
            # slowest one. Bounded so a pathological turn can't fan out freely.
            sem = asyncio.Semaphore(max(1, config.MAX_PARALLEL_TOOLS))

            async def _invoke(idx: int, call: dict[str, Any]):
                t0 = time.time()
                async with sem:
                    try:
                        out = await dispatch(call["name"], user_id, call["arguments"])
                        status = "error" if isinstance(out, dict) and "error" in out else "ok"
                    except Exception as exc:  # a tool must never crash the run
                        out = {"error": str(exc)}
                        status = "error"
                return idx, out, status, (time.time() - t0) * 1000

            tasks = [asyncio.create_task(_invoke(i, c)) for i, c in enumerate(tool_calls)]
            outputs: list[Any] = [None] * len(tool_calls)
            try:
                # Events are emitted as each tool lands, so the UI updates in
                # completion order rather than waiting for the slowest.
                for fut in asyncio.as_completed(tasks):
                    idx, out, status, ms = await fut
                    outputs[idx] = out
                    call = tool_calls[idx]
                    ev = events.make_event(user_id, call["name"], status, ms, call["arguments"], out)
                    yield {"type": "tool", "event": ev}
                    await events.push(ev)
            finally:
                # A client disconnect closes this generator mid-iteration; without
                # this, in-flight tools would keep running detached.
                for t in tasks:
                    if not t.done():
                        t.cancel()

            # Results go back in CALL order regardless of completion order —
            # providers match tool_call_id positionally and reject a shuffle.
            for call, out in zip(tool_calls, outputs):
                convo.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(out if out is not None else {"error": "tool did not complete"}),
                })

        yield {"type": "text", "text": "\n\n_(reached tool-call limit — try narrowing the question.)_"}
        yield {"type": "done"}
    except Exception as exc:
        yield {"type": "error", "message": str(exc)}


async def close() -> None:
    await llm.close()
