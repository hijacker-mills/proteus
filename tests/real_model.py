"""
End-to-end test against a REAL model and REAL tools.

The mock harness proves the gateway's plumbing; this proves the parts that only
a real model exercises: that it actually emits several tool calls in one turn,
that Proteus dispatches them concurrently, that the multi-turn loop converges,
and what streaming really costs.

The parallelism proof is timing-based: tool events are grouped by turn, then each
batch is checked for how far apart its events ARRIVE. Serial dispatch cannot
produce a spread below (sum of tool times - slowest tool).

Needs a RUNNING proteus with a real MODEL (e.g. anthropic/claude-sonnet-5) and a
toolset that includes web_search.

Run:  set -a; . ./.env; set +a; .venv/bin/python tests/real_model.py
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import httpx

BASE = f"http://127.0.0.1:{os.environ.get('PORT','18791')}"
KEY = os.environ.get("API_KEY", "")
HDR = {"Authorization": f"Bearer {KEY}", "X-Proteus-User-Id": "realtest"}
results: list[tuple[bool, str]] = []


def check(ok, label, detail=""):
    results.append((ok, label))
    print(f"  {'✓' if ok else '✗ FAIL'}  {label}{('  — ' + detail) if detail else ''}")


async def stream(c, prompt, profile=None):
    """Returns (text, tool_events, ttft, total, first_tool_at)."""
    hdr = dict(HDR)
    if profile:
        hdr["X-Proteus-Profile"] = profile
    text, tevs = [], []
    t0 = time.time(); ttft = None; first_tool = None
    async with c.stream("POST", f"{BASE}/v1/chat/completions", headers=hdr,
                        json={"stream": True, "messages": [{"role": "user", "content": prompt}]}) as r:
        if r.status_code != 200:
            body = await r.aread()
            raise RuntimeError(f"HTTP {r.status_code}: {body[:200]}")
        async for line in r.aiter_lines():
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            d = json.loads(line[5:])
            if "proteus_tool_event" in d:
                if first_tool is None:
                    first_tool = time.time() - t0
                ev = d["proteus_tool_event"]
                ev["_arrived"] = time.time() - t0        # when the client saw it
                tevs.append(ev)
            else:
                ct = d["choices"][0]["delta"].get("content")
                if ct:
                    if ttft is None:
                        ttft = time.time() - t0
                    text.append(ct)
    return "".join(text), tevs, ttft, time.time() - t0, first_tool


async def main() -> int:
    async with httpx.AsyncClient(timeout=300) as c:
        h = (await c.get(f"{BASE}/healthz")).json()
        model = h["model"]
        print(f"  model={model}  toolset={h['toolset']}")
        if model.startswith("mock/"):
            print("  ! point MODEL at a real provider first"); return 1

        print("== 1. real streaming: TTFT and token rate ==")
        txt, tevs, ttft, total, _ = await stream(c, "Explain what a load balancer does, in about 120 words.")
        rate = (len(txt) / 4) / total if total else 0
        check(len(txt) > 200, "got a substantial answer", f"{len(txt)} chars")
        check(ttft is not None and ttft < 15, "time to first token", f"{ttft*1000:.0f}ms")
        print(f"     total {total:.2f}s  ≈{rate:.0f} tok/s output")

        print("== 2. the model emits SEVERAL tools in one turn, run concurrently ==")
        # web_fetch on real URLs — genuinely takes time, unlike a stub.
        # Comparing sum(tool ms) against how far apart the events ARRIVE is the
        # measurement that distinguishes parallel from sequential: run serially,
        # the arrivals spread out by the sum; run together, by roughly the
        # slowest one. Total turn time is useless here, since it is dominated by
        # the model's own thinking across round-trips, not by the tools.
        txt2, tevs2, _, total2, _ = await stream(
            c,
            "Fetch these three pages and give me one line on each: "
            "https://example.com , https://www.iana.org/help/example-domains , "
            "https://httpbin.org/html . Fetch all three.")
        print(f"     {len(tevs2)} tool events: {[t['tool'] for t in tevs2]}")
        for t in tevs2:
            print(f"       {t['tool']:12} {t['status']:5} {t['ms']:6}ms  arrived +{t['_arrived']*1000:.0f}ms")
        check(len(tevs2) >= 2, "model issued multiple tool calls in one turn", f"{len(tevs2)}")

        # Events must be grouped BY TURN before timing them. The agent loop can
        # take several round-trips (the model retries a failed tool), and tools
        # from different turns are separated by a model call, so pooling them
        # all into one "spread" measures the model's thinking, not dispatch.
        # A gap far larger than any single tool means a new turn started.
        batches, cur = [], [tevs2[0]] if tevs2 else []
        for prev, t in zip(tevs2, tevs2[1:]):
            if (t["_arrived"] - prev["_arrived"]) * 1000 > 1000:
                batches.append(cur); cur = [t]
            else:
                cur.append(t)
        if cur:
            batches.append(cur)
        print(f"     {len(batches)} turn(s), batch sizes {[len(b) for b in batches]}")

        multi = [b for b in batches if len(b) >= 2]
        check(bool(multi), "at least one turn carried a batch of tools", f"{[len(b) for b in batches]}")
        for b in multi:
            tool_sum = sum(t["ms"] for t in b)
            slowest = max(t["ms"] for t in b)
            spread = (max(t["_arrived"] for t in b) - min(t["_arrived"] for t in b)) * 1000
            sequential_floor = tool_sum - slowest      # serial dispatch cannot beat this
            print(f"     batch of {len(b)}: sum={tool_sum}ms slowest={slowest}ms "
                  f"spread={spread:.0f}ms  (sequential would be >={sequential_floor}ms)")
            check(spread < max(sequential_floor * 0.6, 40),
                  f"batch of {len(b)} dispatched concurrently",
                  f"spread {spread:.0f}ms vs sequential floor {sequential_floor}ms")
        check(len(txt2) > 50, "model produced a final answer after the tools", f"{len(txt2)} chars")

        print("== 3. multi-turn tool loop converges ==")
        check(all(t["status"] in ("ok", "error") for t in tevs2), "every tool event has a clean status")
        check("example" in txt2.lower() or "domain" in txt2.lower() or "herman" in txt2.lower(),
              "the answer reflects what was fetched", txt2[:70].replace("\n", " "))

        print("== 4. concurrent real requests ==")
        t0 = time.time()
        outs = await asyncio.gather(*[
            stream(c, f"In one sentence, name fact number {i} about the ocean.") for i in range(6)
        ], return_exceptions=True)
        wall = time.time() - t0
        good = [o for o in outs if not isinstance(o, Exception)]
        check(len(good) == 6, "6 concurrent real completions all succeeded",
              f"{len(good)}/6 in {wall:.1f}s")
        if good:
            longest = max(o[3] for o in good)
            check(wall < longest * 2.5, "they overlapped rather than queueing",
                  f"wall {wall:.1f}s vs slowest single {longest:.1f}s")

        h2 = (await c.get(f"{BASE}/healthz")).json()
        check(h2["concurrency"]["rejected"] == 0, "nothing shed", str(h2["concurrency"]))

    ok = all(o for o, _ in results)
    print(f"\n{'ALL PASSED' if ok else 'FAILURES'}: {sum(1 for o,_ in results if o)}/{len(results)}")
    for o, l in results:
        if not o:
            print(f"  failed: {l}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
