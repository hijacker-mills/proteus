"""
Proves tool calls in one turn run concurrently, and that results still reach the
model in CALL order.

Both properties matter and they pull against each other. Concurrency is the
latency win; call order is a correctness requirement, because providers match
tool results to `tool_call_id` positionally and reject a shuffled list. A naive
"just gather it" change gets the first and silently breaks the second, and the
breakage only surfaces against a real provider.

The ordering test deliberately makes the LAST call finish FIRST, so completion
order and call order disagree and the assertion has something to catch.

Run:  set -a; . ./.env; set +a; .venv/bin/python tests/tool_parallel.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on sys.path

from app import agent, config

DELAY = 0.4
N = 5
_TOOLS_SCHEMA = [{"function": {"name": "x"}}]

second_pass_messages: list[list[dict]] = []


def make_astream():
    async def fake_astream(model, messages, tools, max_tokens, temperature, reasoning_effort=None):
        if any(m.get("role") == "tool" for m in messages):
            second_pass_messages.append(list(messages))       # what the model actually sees
            yield {"type": "text", "text": "done"}
            yield {"type": "final", "tool_calls": [], "finish_reason": "stop"}
            return
        yield {
            "type": "final",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {"id": f"call_{i}", "name": f"tool_{i}", "arguments": {"n": i}}
                for i in range(N)
            ],
        }
    return fake_astream


async def main() -> int:
    agent.llm.astream = make_astream()
    ok = True

    # ---- 1. concurrency -----------------------------------------------------
    async def uniform(name, user_id, args):
        await asyncio.sleep(DELAY)
        return {"tool": name}

    agent.profiles.resolve = lambda n, host_tools=False: ("sys", _TOOLS_SCHEMA, uniform)
    t0 = time.time()
    events = [e async for e in agent.run("u1", [{"role": "user", "content": "go"}])]
    elapsed = time.time() - t0
    tool_events = [e for e in events if e["type"] == "tool"]
    sequential = DELAY * N

    print(f"  {N} tools x {DELAY}s, MAX_PARALLEL_TOOLS={config.MAX_PARALLEL_TOOLS}")
    print(f"  sequential would be ~{sequential:.1f}s; actual {elapsed:.2f}s")
    if elapsed >= sequential * 0.8:
        print(f"  FAIL: {elapsed:.2f}s is still sequential"); ok = False
    else:
        print(f"  ✓ concurrent — {sequential/elapsed:.1f}x faster than sequential")
    if len(tool_events) != N:
        print(f"  FAIL: expected {N} tool events, got {len(tool_events)}"); ok = False
    else:
        print("  ✓ every tool emitted an event")

    # ---- 2. call order preserved despite reversed completion ----------------
    completion: list[str] = []

    async def reversed_finish(name, user_id, args):
        # call_0 sleeps longest, call_4 returns almost immediately
        await asyncio.sleep(DELAY * (N - args["n"]) / N)
        completion.append(name)
        return {"tool": name}

    second_pass_messages.clear()
    agent.profiles.resolve = lambda n, host_tools=False: ("sys", _TOOLS_SCHEMA, reversed_finish)
    [e async for e in agent.run("u1", [{"role": "user", "content": "go"}])]

    if not second_pass_messages:
        print("  FAIL: model never received a second pass"); return 1
    sent_ids = [m["tool_call_id"] for m in second_pass_messages[0] if m.get("role") == "tool"]
    expected = [f"call_{i}" for i in range(N)]

    print(f"  completed in order : {completion}")
    print(f"  sent to model as   : {sent_ids}")
    if completion == [f"tool_{i}" for i in range(N)]:
        print("  WARN: completion matched call order, so this run proves little")
    if sent_ids != expected:
        print(f"  FAIL: results reached the model out of order, expected {expected}"); ok = False
    else:
        print("  ✓ results reached the model in call order despite finishing reversed")

    # ---- 3. volatile clock is LAST in the system prompt ---------------------
    sys_msg = next(m for m in second_pass_messages[0] if m["role"] == "system")
    body = sys_msg["content"]
    if "Current time:" not in body:
        print("  FAIL: no timestamp in system prompt"); ok = False
    else:
        tail = body[body.index("Current time:"):]
        if "\n\n" in tail.rstrip():
            print("  FAIL: content follows the timestamp — cache prefix still broken"); ok = False
        else:
            print("  ✓ timestamp is the final block, so the prefix above it is cacheable")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
