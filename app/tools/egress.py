"""
Egress proxy: the one way a URL the model chose leaves the gateway.

`url_safety.is_safe_url` checks a URL before a fetch, and that is not enough on
its own. Three things get past a check that runs once, up front:

  redirects     a public page answers 302 to http://169.254.169.254/. httpx
                and Chromium both follow it without asking again.
  subrequests   a page the browser renders loads images, scripts, iframes and
                fetch() calls of its own, none of which the tool ever sees.
  rebinding     a hostname resolves to a public address for the check and to
                an internal one a moment later, when the client connects.

Chromium also skips Playwright's request routing for redirect hops (measured on
Playwright 1.62: a routed page that 302s to an internal host loads it), so the
browser cannot be policed from inside Playwright either.

So both the browser and the `web_fetch` client send their traffic through this
proxy. For every connection it resolves the host once, checks every address
with `url_safety.vetted_addresses`, and dials one of those addresses itself.
Every redirect hop and every subrequest is a new connection through here, and
the address that was checked is the address that gets connected to.

It runs inside each worker process on 127.0.0.1 with an ephemeral port, started
on first use. It speaks just enough HTTP/1.1 to proxy: CONNECT for https (the
TLS stays end to end, so certificates are still checked by the client) and
absolute-form requests for plain http.
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .url_safety import vetted_addresses

logger = logging.getLogger("proteus.tools.egress")

_HEAD_LIMIT = 64 * 1024
_HEAD_TIMEOUT = 30
_CONNECT_TIMEOUT = 10
_TUNNEL_LIFETIME = 300

# Hop-by-hop headers a proxy must not forward. Connection is replaced with
# "close" so one client connection never carries a second request to a host
# that wasn't checked.
_DROP_HEADERS = {b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive"}

_server: asyncio.base_events.Server | None = None
_server_loop: asyncio.AbstractEventLoop | None = None
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _split_host_port(authority: str, default_port: int) -> tuple[str, int]:
    if authority.startswith("["):                      # [v6]:port
        host, _, rest = authority[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = authority.rpartition(":") if authority.count(":") == 1 else (authority, "", "")
    return host, int(port) if port else default_port


def _refuse(writer: asyncio.StreamWriter, status: str, reason: str) -> None:
    body = reason.encode()
    writer.write(
        f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    )


async def _dial(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    addresses = await asyncio.to_thread(vetted_addresses, host)
    if not addresses:
        return None
    last: Exception | None = None
    for address in addresses:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), _CONNECT_TIMEOUT)
        except Exception as exc:                        # try the next vetted address
            last = exc
    raise ConnectionError(f"could not connect to {host}:{port}: {last}")


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except Exception:
        pass
    finally:
        try:
            if dst.can_write_eof():
                dst.write_eof()
        except Exception:
            pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    upstream: asyncio.StreamWriter | None = None
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT)
        request_line, _, header_block = head[:-4].partition(b"\r\n")
        method, target, version = request_line.decode("latin-1").split(" ", 2)

        if method == "CONNECT":
            host, port = _split_host_port(target, 443)
            path = ""
        else:
            parsed = httpx.URL(target)
            if parsed.scheme != "http" or not parsed.host:
                _refuse(writer, "400 Bad Request", "absolute http:// URL required")
                return
            host, port = parsed.host, parsed.port or 80
            path = parsed.raw_path.decode("latin-1") or "/"

        dialled = await _dial(host, port)
        if dialled is None:
            _refuse(writer, "403 Forbidden",
                    f"refused: {host} resolves to a private or metadata address")
            return
        up_reader, upstream = dialled

        if method == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            lines = [f"{method} {path} {version}".encode("latin-1")]
            for line in header_block.split(b"\r\n"):
                name = line.split(b":", 1)[0].strip().lower()
                if line and name not in _DROP_HEADERS:
                    lines.append(line)
            lines.append(b"Connection: close")
            upstream.write(b"\r\n".join(lines) + b"\r\n\r\n")
        await writer.drain()

        await asyncio.wait_for(
            asyncio.gather(_pump(reader, upstream), _pump(up_reader, writer)),
            _TUNNEL_LIFETIME,
        )
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError):
        pass
    except Exception as exc:
        logger.warning("egress proxy: %s", exc)
        try:
            _refuse(writer, "502 Bad Gateway", "upstream connection failed")
        except Exception:
            pass
    finally:
        for w in (upstream, writer):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass


async def proxy_url() -> str:
    """Start the proxy for this event loop if it isn't running, return its URL."""
    global _server, _server_loop, _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _server is not None and _server_loop is loop:
        return _url()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    async with _lock:
        if _server is None or _server_loop is not loop:
            _server = await asyncio.start_server(_handle, "127.0.0.1", 0, limit=_HEAD_LIMIT)
            _server_loop = loop
            logger.info("egress proxy listening on %s", _url())
    return _url()


def _url() -> str:
    assert _server is not None
    port = _server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}"


async def get_client() -> httpx.AsyncClient:
    """An httpx client whose every request, redirect hop included, goes through the proxy."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        _client = httpx.AsyncClient(
            proxy=await proxy_url(),
            trust_env=False,
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=0),
        )
        _client_loop = loop
    return _client


async def close() -> None:
    global _server, _server_loop, _client, _client_loop
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = _client_loop = None
    if _server is not None:
        _server.close()
        _server = _server_loop = None
