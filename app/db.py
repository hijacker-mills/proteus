"""
asyncpg connection pool. Works against a plain local Postgres or a managed,
pooled endpoint (Neon, Supabase, RDS Proxy).

Each uvicorn worker process gets its own pool. Behind a pooled endpoint,
PgBouncer multiplexes these onto a small set of backend connections, so many
workers × a modest max_size stays well under the provider's backend cap.

CRITICAL: PgBouncer transaction pooling does not support server-side prepared
statements, so `statement_cache_size=0` is mandatory or queries fail randomly
under load. It is harmless on a direct connection, so it is always set.

THE DATABASE IS OPTIONAL. `/v1/chat/completions` is stateless — the transcript
arrives in the request body — so chat, tools and streaming all work with no
Postgres at all. Only memory, channels and scheduled jobs need it. Callers
therefore start the pool with `try_init_pool()` and run degraded if it fails,
with `start_reconnector()` healing the process in the background once the
database comes back. Anything DB-backed must gate on `available()` first.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg

from . import config

logger = logging.getLogger("proteus.db")

_pool: asyncpg.Pool | None = None
_reconnector: asyncio.Task | None = None


def _clean_dsn(dsn: str) -> str:
    """Strip libpq-only query params (sslmode) that asyncpg handles via kwargs."""
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _ssl_for(dsn: str) -> ssl.SSLContext | None:
    """TLS for a managed provider, none for a local server that has no cert.

    Read off the ORIGINAL dsn, because `_clean_dsn` throws `sslmode` away before
    asyncpg ever sees it. A loopback host counts as plaintext too: the local
    Postgres container serves no certificate, and asyncpg's default context
    would reject the handshake rather than fall back.
    """
    parts = urlsplit(dsn)
    if parse_qs(parts.query).get("sslmode", [""])[0].lower() in ("disable", "allow"):
        return None
    if (parts.hostname or "").lower() in ("localhost", "127.0.0.1", "::1"):
        return None
    return ssl.create_default_context()


async def init_pool() -> asyncpg.Pool:
    """Open the shared pool, raising if the database is unreachable."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=_clean_dsn(config.DATABASE_URL),
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            ssl=_ssl_for(config.DATABASE_URL),
            statement_cache_size=0,  # required for PgBouncer transaction pooling
            command_timeout=15,
        )
    return _pool


async def try_init_pool(*, quiet: bool = False) -> bool:
    """Open the pool, logging and swallowing failure.

    Returns whether a pool is available. A False here is not fatal: the process
    runs without memory/channels/cron until `start_reconnector()` succeeds.
    `quiet` drops the failure to DEBUG so retry loops don't flood the log.
    """
    level = logging.DEBUG if quiet else logging.WARNING
    if not config.DATABASE_URL:
        logger.log(level, "DATABASE_URL is unset — running stateless (no memory, channels or cron)")
        return False
    try:
        await init_pool()
        return True
    except Exception as exc:
        logger.log(level, "database unavailable (%s) — running stateless until it returns", exc)
        return False


def available() -> bool:
    """True when a pool exists. Gate every DB-backed feature on this."""
    return _pool is not None


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
    if _pool is None:
        return False
    try:
        async with get_pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False


_RECONNECT_MAX = 600  # a quota outage can last days; stop hammering after 10 min


async def _reconnect_loop(interval: int, on_connect: Callable[[], Awaitable[None]] | None) -> None:
    delay = interval
    while not available():
        await asyncio.sleep(delay)
        if await try_init_pool(quiet=True):
            logger.info("database reconnected — DB-backed features re-enabled")
            if on_connect is not None:
                try:
                    await on_connect()
                except Exception:
                    logger.exception("post-reconnect hook failed")
            return
        if delay < _RECONNECT_MAX:
            delay = min(delay * 2, _RECONNECT_MAX)
            logger.warning("database still unavailable — next retry in %ds", delay)


async def start_reconnector(interval: int = 60,
                            on_connect: Callable[[], Awaitable[None]] | None = None) -> None:
    """Retry the pool in the background until it opens, then run `on_connect`.

    Lets a process that booted during an outage (an exhausted compute quota, a
    database still starting) heal on its own instead of needing a restart.
    """
    global _reconnector
    if available() or _reconnector is not None:
        return
    _reconnector = asyncio.create_task(_reconnect_loop(interval, on_connect))


async def stop_reconnector() -> None:
    global _reconnector
    if _reconnector is not None:
        _reconnector.cancel()
        try:
            await _reconnector
        except (asyncio.CancelledError, Exception):
            pass
        _reconnector = None
