"""
Shared channel helpers, modeled on patterns from Hermes's platform adapters:
  - inbound idempotency / dedup (webhook retries, redelivery)
  - per-session serialization (no concurrent agent runs on one conversation)
  - bounded send retry for transient platform errors
  - long-message chunking
"""
from __future__ import annotations

import asyncio
import base64
import time
from collections import OrderedDict
from typing import Awaitable, Callable, TypeVar

from ..redisc import get_redis

T = TypeVar("T")

_DEDUP_TTL = 3600
_seen: "OrderedDict[str, float]" = OrderedDict()
_locks: dict[str, asyncio.Lock] = {}


async def already_processed(dedup_id: str) -> bool:
    """True if this id was seen before (within TTL). Records it otherwise.
    Uses Redis (SET NX) when available so dedup holds across web workers."""
    client = await get_redis()
    if client is not None:
        ok = await client.set(f"acag:seen:{dedup_id}", "1", nx=True, ex=_DEDUP_TTL)
        return not ok  # set() returns None if the key already existed
    now = time.time()
    while _seen and next(iter(_seen.values())) < now - _DEDUP_TTL:
        _seen.popitem(last=False)
    if dedup_id in _seen:
        return True
    _seen[dedup_id] = now
    if len(_seen) > 5000:
        _seen.popitem(last=False)
    return False


def session_lock(key: str) -> asyncio.Lock:
    """One asyncio.Lock per session key — serializes messages within a conversation."""
    lk = _locks.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _locks[key] = lk
    return lk


async def send_with_retry(fn: Callable[[], Awaitable[T]], attempts: int = 3, base_delay: float = 1.0) -> T:
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — transient platform/network errors
            last = exc
            if i < attempts - 1:
                await asyncio.sleep(base_delay * (2 ** i))
    raise last  # type: ignore[misc]


def data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    """Encode downloaded media bytes as a base64 data URL for the vision model."""
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def chunks(text: str, size: int = 4000) -> list[str]:
    """Split into <=size pieces, preferring newline boundaries."""
    if len(text) <= size:
        return [text]
    out, rest = [], text
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        out.append(rest)
    return out
