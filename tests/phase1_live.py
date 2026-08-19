"""
Live-server half of the Phase 1 verification: the things that only fail over a
real socket — orjson on the wire, coalescing with tool events interleaved, and
slot release when a client hangs up mid-stream.

Needs a RUNNING proteus with MODEL=mock/* and WORKERS=1. Workers matter: the
concurrency limiter is per-process, so with 4 workers /healthz may answer from a
different process than the one that served the stream, and the leak check would
pass without proving anything.

Run:  set -a; . ./.env; set +a; .venv/bin/python tests/phase1_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = f"http://127.0.0.1:{os.environ.get('PORT','18791')}"
KEY = os.environ.get("API_KEY", "")
HDR = {"Authorization": f"Bearer {KEY}", "X-Proteus-User-Id": "phase1"}
results: list[tuple[bool, str]] = []


def check(ok, label, detail=""):
    results.append((ok, label))
    print(f"  {'✓' if ok else '✗ FAIL'}  {label}{('  — ' + detail) if detail else ''}")


async def collect(client, body, hdr=None):
    """Return (text, tool_events, interleave_order, status)."""
    text, tevs, order = [], [], []
    async with client.stream("POST", f"{BASE}/v1/chat/completions",
                             headers=hdr or HDR, json=body) as r:
        if r.status_code != 200:
            await r.aread()
            return "", [], "", r.status_code
        async for line in r.aiter_lines():
            if not line.startswith("data:") or line == "data: [DONE]":
                continue
            d = json.loads(line[5:])
            if "proteus_tool_event" in d:
                tevs.append(d["proteus_tool_event"]); order.append("T")
            else:
                content = d["choices"][0]["delta"].get("content")
                if content:
                    text.append(content); order.append("t")
    return "".join(text), tevs, "".join(order), 200


async def main() -> int:
    from app import main as appmain          # for the _dumps unit check

    async with httpx.AsyncClient(timeout=180) as c:
        health = (await c.get(f"{BASE}/healthz")).json()
        model = health["model"]
        print(f"  server model={model}")
        if not model.startswith("mock/"):
            print("  ! set MODEL=mock/fast before running this"); return 1

        print("== 1. orjson: real non-ASCII, on the wire ==")
        # orjson emits raw UTF-8 where stdlib json escapes to \uXXXX. Both are
        # valid JSON but the BYTES differ, so this needs actual non-ASCII.
        probe = {"a": "⚠️ émoji 🎉 中文  ", "b": 'quote " and \\ backslash'}
        wire = appmain._dumps(probe)
        check(json.loads(wire) == probe, "unicode + escapes round-trip through _dumps",
              f"{len(wire)} bytes")
        check(appmain._dumps.__module__ == "app.main", "using the server's own encoder")

        # The error path emits a real ⚠️ over SSE. Trigger it with no user message.
        text, _, _, code = await collect(c, {"stream": True,
                                             "messages": [{"role": "system", "content": "x"}]})
        check(code in (200, 400), "system-only request handled", str(code))
        if code == 200:
            check("⚠️" in text, "non-ASCII warning glyph survived the SSE round-trip",
                  repr(text[:40]))

        print("== 2. tool events actually interleave with text ==")
        # mock/tool emits a tool call first, then answers — the real two-pass shape.
        r = await c.post(f"{BASE}/v1/chat/completions", headers=HDR,
                         json={"messages": [{"role": "user", "content": "hi"}]})
        check(r.status_code == 200, "non-streaming path 200", str(r.status_code))
        j = r.json()
        check(isinstance(j["choices"][0]["message"]["content"], str), "non-streaming body parses")
        check("proteus_tool_events" in j, "non-streaming carries the tool-event array")

        body = {"stream": True, "messages": [{"role": "user", "content": "hi"}]}
        text_a, tevs_a, order_a, _ = await collect(c, body)
        print(f"     order={order_a[:40]}{'…' if len(order_a) > 40 else ''}  "
              f"tools={[t['tool'] for t in tevs_a]}")
        if tevs_a:
            check(order_a.index("T") < order_a.rindex("t"),
                  "tool event arrives before the final text (real interleave)")
        else:
            print("     (no tool events — run with MODEL=mock/tool to exercise this)")

        print("== 3. coalescing changes framing, never the text ==")
        # Compare byte-for-byte against the same request with coalescing off.
        # (The caller flips STREAM_COALESCE_MS between the two runs.)
        check(len(text_a) > 0, "got a body to compare", f"{len(text_a)} chars")
        Path("/tmp/proteus_phase1_text.txt").write_text(text_a)

        print("== 4. abandoned streams must not leak slots (with real work in flight) ==")
        before = (await c.get(f"{BASE}/healthz")).json()["concurrency"]["in_use"]

        import time as _t
        N = 12
        stop = asyncio.Event()
        peak = 0

        async def abandon():
            # Hold the stream OPEN for a while before hanging up. Breaking on the
            # first frame tears down in milliseconds, which is over before any
            # poll lands — the test then measures nothing and passes vacuously.
            try:
                async with c.stream("POST", f"{BASE}/v1/chat/completions", headers=HDR,
                                    json={"stream": True,
                                          "messages": [{"role": "user", "content": "hi"}]}) as r:
                    t0 = _t.time()
                    async for _ in r.aiter_lines():
                        if _t.time() - t0 > 1.5:
                            break              # hang up MID-answer
            except Exception:
                pass

        async def poll():
            nonlocal peak
            while not stop.is_set():
                try:
                    peak = max(peak, (await c.get(f"{BASE}/healthz")).json()["concurrency"]["in_use"])
                except Exception:
                    pass
                await asyncio.sleep(0.1)

        p = asyncio.create_task(poll())
        await asyncio.gather(*[abandon() for _ in range(N)])
        stop.set(); await p
        for _ in range(30):
            await asyncio.sleep(0.5)
            if (await c.get(f"{BASE}/healthz")).json()["concurrency"]["in_use"] <= before:
                break
        after = (await c.get(f"{BASE}/healthz")).json()["concurrency"]["in_use"]
        print(f"     in_use before={before}  peak_during={peak}  after={after}")
        check(peak >= N * 0.5, "slots were genuinely occupied mid-flight (needs a slow model)",
              f"peak={peak}/{N}")
        check(after <= before, "and every one came back after the hang-ups",
              f"{before} -> {after}")

        print("== 5. still healthy afterwards ==")
        text_b, _, _, code_b = await collect(c, body)
        check(code_b == 200 and len(text_b) > 0, "still streaming normally after the abuse")
        h = (await c.get(f"{BASE}/healthz")).json()
        check(h["status"] in ("ok", "degraded"), "healthz sane", h["status"])
        check(h["concurrency"]["rejected"] == 0, "nothing wrongly shed", str(h["concurrency"]))

    ok = all(o for o, _ in results)
    print(f"\n{'ALL PASSED' if ok else 'FAILURES'}: {sum(1 for o,_ in results if o)}/{len(results)}")
    for o, label in results:
        if not o:
            print(f"  failed: {label}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
