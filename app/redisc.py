"""Shared lazy Redis client (used by tool-event stream and channel sessions)."""
from __future__ import annotations

from . import config

_redis = None


async def get_redis():
    global _redis
    if not config.REDIS_URL:
        return None
    if _redis is None:
        import redis.asyncio as aioredis  # imported lazily so redis stays optional

        _redis = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception:
            pass
        _redis = None
