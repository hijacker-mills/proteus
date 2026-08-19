"""
Per-user rate limiting.

`MAX_CONCURRENT_COMPLETIONS` stops the gateway drowning, but it is a single
global pool: one enthusiastic user can hold every slot and everybody else
queues behind them. That is a fairness problem rather than a capacity one, and
it needs a per-user limit to fix.

A token bucket rather than a fixed window, because a fixed window lets someone
send their whole minute's allowance in the last second and the next minute's in
the first, i.e. double the intended rate across the boundary. A bucket refills
smoothly and permits a deliberate burst up to its size, which is what people
actually want: a few rapid turns, not a sustained flood.

PER WORKER, like the concurrency cap. With `WORKERS=4` the real ceiling is four
times `RATE_LIMIT_PER_MINUTE`, because each process keeps its own buckets. Set
the value accordingly. A cross-worker limit would need Redis on the request
path, which is a real cost for a limit whose job is to stop one user being
antisocial, not to bill anyone.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import config


@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class RateLimiter:
    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        self.rate = per_minute / 60.0            # tokens per second
        self.capacity = float(burst or max(per_minute, 1))
        self.enabled = per_minute > 0
        self._buckets: dict[str, _Bucket] = {}
        self.rejected = 0

    def check(self, key: str) -> tuple[bool, float]:
        """(allowed, retry_after_seconds). Consumes a token when allowed."""
        if not self.enabled:
            return True, 0.0

        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(tokens=self.capacity)

        bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
        bucket.updated = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0

        self.rejected += 1
        # How long until one whole token exists again.
        return False, max(1.0, (1.0 - bucket.tokens) / self.rate)

    def prune(self, max_idle: float = 900.0) -> int:
        """Drop buckets nobody has touched, so a long-lived worker serving many
        one-off users does not accumulate one dict entry per user forever."""
        cutoff = time.monotonic() - max_idle
        stale = [k for k, b in self._buckets.items() if b.updated < cutoff]
        for k in stale:
            del self._buckets[k]
        return len(stale)

    @property
    def tracked(self) -> int:
        return len(self._buckets)


limiter = RateLimiter(config.RATE_LIMIT_PER_MINUTE, config.RATE_LIMIT_BURST)
