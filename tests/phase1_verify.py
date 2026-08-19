"""
Adversarial verification of the Phase 1 changes (parallel tools, prompt-cache
ordering, orjson/coalescing). These are the cases most likely to be broken by
the optimisation rather than the ones most likely to pass.

In-process only — no server, no provider. Run the live half with
tests/phase1_live.py.

Run:  set -a; . ./.env; set +a; .venv/bin/python tests/phase1_verify.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import agent, config, llm

SCHEMA = [{"function": {"name": "x"}}]
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(f"  {'✓' if ok else '✗ FAIL'}  {label}{('  — ' + detail) if detail else ''}")


def scripted(script):
    """script: list of tool_call-lists; [] ends the run with text."""
    state = {"i": 0}
    seen: list[list[dict]] = []

    async def astream(model, messages, tools, max_tokens, temperature, reasoning_effort=None):
        seen.append(list(messages))
        i = state["i"]; state["i"] += 1
        calls = script[i] if i < len(script) else []
        if not calls:
            yield {"type": "text", "text": "final answer"}
            yield {"type": "final", "tool_calls": [], "finish_reason": "stop"}
            return
        yield {"type": "final", "tool_calls": calls, "finish_reason": "tool_calls"}

    return astream, seen


async def run_with(dispatch, script, extra_system=""):
    astream, seen = scripted(script)
    agent.llm.astream = astream
    agent.profiles.resolve = lambda n, host_tools=False: ("PERSONA", SCHEMA, dispatch)
    evs = [e async for e in agent.run("u", [{"role": "user", "content": "go"}], extra_system=extra_system)]
    return evs, seen


def calls(*names):
    return [{"id": f"call_{n}", "name": n, "arguments": {"n": i}} for i, n in enumerate(names)]


async def main() -> int:
    print("== 1. multi-turn tool loop (tool -> model -> tool -> model) ==")
    hits: list[str] = []

    async def d(name, uid, args):
        hits.append(name)
        return {"ok": name}

    evs, seen = await run_with(d, [calls("a", "b"), calls("c"), []])
    tool_evs = [e for e in evs if e["type"] == "tool"]
    check(len(tool_evs) == 3, "3 tool events across 2 tool turns", f"got {len(tool_evs)}")
    check(sorted(hits) == ["a", "b", "c"], "every tool actually dispatched", str(hits))
    check(any(e["type"] == "done" for e in evs), "run finished with done")
    # turn 3 must carry results of BOTH previous turns
    last = seen[-1]
    tool_msgs = [m for m in last if m.get("role") == "tool"]
    check(len(tool_msgs) == 3, "all 3 tool results carried into the final prompt", f"got {len(tool_msgs)}")
    ids = [m["tool_call_id"] for m in tool_msgs]
    check(ids == ["call_a", "call_b", "call_c"], "ids in call order across turns", str(ids))

    print("== 2. MAX_PARALLEL_TOOLS=1 must serialise ==")
    orig = config.MAX_PARALLEL_TOOLS
    config.MAX_PARALLEL_TOOLS = 1
    live = {"now": 0, "max": 0}

    async def slow(name, uid, args):
        live["now"] += 1; live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.15)
        live["now"] -= 1
        return {"ok": name}

    t0 = time.time()
    await run_with(slow, [calls("a", "b", "c"), []])
    serial_time = time.time() - t0
    check(live["max"] == 1, "cap of 1 held — never more than 1 tool in flight", f"peak={live['max']}")
    check(serial_time >= 0.4, "took the serial time", f"{serial_time:.2f}s")

    config.MAX_PARALLEL_TOOLS = 8
    live = {"now": 0, "max": 0}
    t0 = time.time()
    await run_with(slow, [calls("a", "b", "c"), []])
    par_time = time.time() - t0
    check(live["max"] == 3, "cap of 8 allowed all 3 concurrently", f"peak={live['max']}")
    check(par_time < serial_time / 2, "and it was faster", f"{par_time:.2f}s vs {serial_time:.2f}s")
    config.MAX_PARALLEL_TOOLS = orig

    print("== 3. a raising tool must not kill the run ==")
    async def boom(name, uid, args):
        if name == "b":
            raise RuntimeError("tool exploded")
        return {"ok": name}

    evs, seen = await run_with(boom, [calls("a", "b", "c"), []])
    tool_evs = [e for e in evs if e["type"] == "tool"]
    errs = [e for e in tool_evs if e["event"]["status"] == "error"]
    check(len(tool_evs) == 3, "all 3 tools still reported", f"got {len(tool_evs)}")
    check(len(errs) == 1 and "exploded" in errs[0]["event"].get("error", ""),
          "the failure surfaced as an error event", str(errs[0]["event"].get("error"))[:40] if errs else "none")
    check(any(e["type"] == "done" for e in evs), "run still completed")
    tool_msgs = [m for m in seen[-1] if m.get("role") == "tool"]
    check([m["tool_call_id"] for m in tool_msgs] == ["call_a", "call_b", "call_c"],
          "order intact even with a failure in the middle")

    print("== 4. non-dict tool return ==")
    async def weird(name, uid, args):
        return ["not", "a", "dict"]

    evs, _ = await run_with(weird, [calls("a"), []])
    check(any(e["type"] == "done" for e in evs), "non-dict return did not crash the run")

    print("== 5. prompt layout: stable first, clock last ==")
    async def noop(name, uid, args):
        return {"ok": 1}

    evs, seen = await run_with(noop, [[]], extra_system="MODE BLOCK HERE")
    sysmsg = next(m for m in seen[0] if m["role"] == "system")["content"]
    check(sysmsg.startswith("PERSONA"), "persona is first")
    check("MODE BLOCK HERE" in sysmsg, "mode block still injected")
    check(sysmsg.index("MODE BLOCK HERE") < sysmsg.index("Current time:"),
          "mode block sits BEFORE the clock (inside the cacheable prefix)")
    check(sysmsg.rstrip().endswith("UTC."), "clock is the very last thing in the prompt")
    stable = sysmsg[:sysmsg.index(llm.CLOCK_PREFIX)]
    check("PERSONA" in stable and "MODE BLOCK HERE" in stable,
          "cacheable prefix contains persona + mode block")

    print("== 6. cache breakpoint only fires for explicit-cache providers ==")
    msgs = [{"role": "system", "content": "S" + llm.CLOCK_PREFIX + "now."}]
    m2, t2 = llm._apply_cache_breakpoint(msgs, [{"function": {"name": "a"}}, {"function": {"name": "b"}}])
    check(isinstance(m2[0]["content"], list) and len(m2[0]["content"]) == 2, "system split into 2 blocks")
    check("cache_control" in m2[0]["content"][0] and "cache_control" not in m2[0]["content"][1],
          "breakpoint on the stable block only, not the clock")
    check("cache_control" in t2[-1] and "cache_control" not in t2[0], "breakpoint on last tool schema only")
    check(isinstance(msgs[0]["content"], str), "input messages not mutated")
    check(llm._EXPLICIT_CACHE_PREFIXES and not "openai/gpt-4o".startswith(llm._EXPLICIT_CACHE_PREFIXES),
          "openai is NOT sent a breakpoint (it caches automatically)")
    check("anthropic/claude-sonnet-4-6".startswith(llm._EXPLICIT_CACHE_PREFIXES),
          "anthropic IS matched")

    print("== 7. no-clock prompt still handled ==")
    m3, _ = llm._apply_cache_breakpoint([{"role": "system", "content": "no clock here"}], None)
    check(len(m3[0]["content"]) == 1 and "cache_control" in m3[0]["content"][0],
          "whole system cached when there is no volatile tail")

    ok = all(o for o, _ in results)
    print(f"\n{'ALL PASSED' if ok else 'FAILURES'}: {sum(1 for o,_ in results if o)}/{len(results)}")
    if not ok:
        for o, label in results:
            if not o:
                print(f"  failed: {label}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
