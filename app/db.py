"""
asyncpg connection pool for Neon (PgBouncer / pooled endpoint).

Each uvicorn worker process gets its own pool. With the pooled `-pooler` Neon
endpoint, PgBouncer multiplexes these onto a small set of backend connections,
so many workers × a modest max_size stays well under Neon's backend cap.

CRITICAL: PgBouncer transaction pooling does not support server-side prepared
statements, so `statement_cache_size=0` is mandatory or queries fail randomly
under load.
"""
from __future__ import annotations

import ssl
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from . import config

_pool: asyncpg.Pool | None = None


def _clean_dsn(dsn: str) -> str:
    """Strip libpq-only query params (sslmode) that asyncpg handles via kwargs."""
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def init_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        ctx = ssl.create_default_context()
        _pool = await asyncpg.create_pool(
            dsn=_clean_dsn(config.DATABASE_URL),
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            ssl=ctx,
            statement_cache_size=0,  # required for PgBouncer transaction pooling
            command_timeout=15,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool


async def healthcheck() -> bool:
    try:
        async with get_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
