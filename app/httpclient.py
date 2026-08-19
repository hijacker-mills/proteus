"""Shared async HTTP clients (connection pooling across all outbound calls).

Two clients, because they want opposite timeouts:

  get_client()        tools and webhooks. Short timeouts — a slow web page must
                      not hold a chat turn open.
  get_stream_client() model completions. A long read timeout, since a streaming
                      answer legitimately takes minutes, but still pooled: a
                      fresh AsyncClient per completion would pay a new TCP + TLS
                      handshake every turn and throw its pool away, which is
                      pure latency once you have real concurrency.
"""
from __future__ import annotations

import httpx

from . import config

_client: httpx.AsyncClient | None = None
_stream_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
    return _client


def get_stream_client() -> httpx.AsyncClient:
    """Pooled client for long-lived streaming completions."""
    global _stream_client
    if _stream_client is None:
        _stream_client = httpx.AsyncClient(
            # `read` bounds the gap BETWEEN chunks, not the whole stream, so a
            # healthy slow answer is fine and a dead connection still trips.
            timeout=httpx.Timeout(config.REQUEST_TIMEOUT, connect=10.0, read=120.0),
            limits=httpx.Limits(max_connections=512, max_keepalive_connections=128),
        )
    return _stream_client


async def close_client() -> None:
    global _client, _stream_client
    for name in ("_client", "_stream_client"):
        c = globals()[name]
        if c is not None:
            await c.aclose()
            globals()[name] = None
