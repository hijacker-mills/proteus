"""
Concurrency load test for proteus.

Fires N concurrent chat requests and reports success rate + latency percentiles.
This measures the GATEWAY's concurrency behaviour; remember the real ceiling at
high N is the upstream provider's rate limit, not proteus itself.

Usage:
    .venv/bin/python tests/loadtest.py --n 100 --concurrency 50
    .venv/bin/python tests/loadtest.py --n 2000 --concurrency 500   # stress
"""
from __future__ import annotations

import argparse
import asyncio
import os
import time

import httpx

from app.identity import signed_headers

USERS = ["loadtest-1", "loadtest-2", "loadtest-3", "loadtest-4"]
PROMPTS = [
    "Give me one quick productivity tip. Be brief.",
    "Name one interesting fact about the ocean. One sentence.",
    "What is 17 * 23? Answer with the number only.",
    "Summarise what a load balancer does. One sentence.",
]


async def one(client: httpx.AsyncClient, base: str, api_key: str, i: int) -> tuple[bool, float]:
    t0 = time.time()
    try:
        r = await client.post(
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", **signed_headers(USERS[i % len(USERS)])},
            json={"messages": [{"role": "user", "content": PROMPTS[i % len(PROMPTS)]}]},
        )
        ok = r.status_code == 200 and bool(r.json()["choices"][0]["message"]["content"])
        return ok, time.time() - t0
    except Exception:
        return False, time.time() - t0


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=25)
    ap.add_argument("--base", default=f"http://127.0.0.1:{os.environ.get('PORT', '18791')}")
    ap.add_argument("--api-key", default=os.environ.get("API_KEY", ""))
    args = ap.parse_args()

    sem = asyncio.Semaphore(args.concurrency)
    results: list[tuple[bool, float]] = []

    async with httpx.AsyncClient(timeout=120) as client:
        async def guarded(i: int):
            async with sem:
                results.append(await one(client, args.base, args.api_key, i))

        print(f"Firing {args.n} requests, up to {args.concurrency} concurrent → {args.base}")
        wall0 = time.time()
        await asyncio.gather(*(guarded(i) for i in range(args.n)))
        wall = time.time() - wall0

    oks = [d for ok, d in results if ok]
    fails = sum(1 for ok, _ in results if not ok)
    lat = sorted(d for _, d in results)

    def pct(p: float) -> float:
        return lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0.0

    print(f"\n  total wall:   {wall:6.2f}s")
    print(f"  throughput:   {args.n / wall:6.2f} req/s")
    print(f"  success:      {len(oks)}/{args.n}  (failures: {fails})")
    print(f"  latency p50:  {pct(0.50):6.2f}s")
    print(f"  latency p95:  {pct(0.95):6.2f}s")
    print(f"  latency p99:  {pct(0.99):6.2f}s")
    print(f"  latency max:  {pct(1.00):6.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
