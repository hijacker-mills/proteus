"""
proteus HTTP surface — OpenAI-compatible, so an existing caller switches over by
changing only the base URL.

  POST /v1/chat/completions   {messages, stream?, user?}   → SSE or JSON
  GET  /healthz                                             → liveness + DB check
  GET  /v1/models                                           → advertises the active model

user_id (the scope for every tool call) is resolved, in priority order:
  1. X-Proteus-User-Id header     (preferred)
  2. body "user" field            (OpenAI's standard field)
  3. "user_id: <id>" in a system message
The model never sees it — it is injected server-side at tool dispatch.

PRIVILEGE: Authorization (API_KEY) admits a caller to chat. It does NOT admit
them to the host. shell/run_code/email/schedule additionally require
X-Proteus-Admin-Key matching ADMIN_API_KEY, which is unset by default — so no HTTP
caller reaches host tools, whichever profile they ask for. Those tools exist for
the operator's allowlisted Telegram bot (see channels/base.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import agent, channels, config, cron, db, httpclient, memory, profiles, redisc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("proteus.http")

# One JSON encode per streamed token, so this is the hottest line in the server.
# orjson is roughly 3-5x faster than stdlib here; the fallback keeps it a soft
# dependency, since a gateway should not fail to start over a speedup.
try:
    import orjson

    def _dumps(payload: dict[str, Any]) -> str:
        return orjson.dumps(payload).decode()
except ImportError:  # pragma: no cover
    def _dumps(payload: dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"))

_USER_RE = re.compile(r"user_id:\s*([^\s]+)", re.IGNORECASE)


class _Limiter:
    """Bounds upstream completions in flight in THIS worker process.

    The gateway holds idle connections cheaply, but every in-flight completion
    burns provider quota. Unbounded, a spike turns into provider 429s for
    everyone including users already mid-stream. Bounded, a spike becomes "some
    users wait a moment", which callers can retry through.

    `slots_in_use` is exported so /healthz can show saturation, which is the
    number you actually scale on.
    """

    def __init__(self, limit: int, wait: float) -> None:
        self._sem = asyncio.Semaphore(limit) if limit > 0 else None
        self._wait = wait
        self.limit = limit
        self.in_use = 0
        self.rejected = 0

    async def acquire(self) -> bool:
        if self._sem is None:
            self.in_use += 1
            return True
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=self._wait)
        except asyncio.TimeoutError:
            self.rejected += 1
            return False
        self.in_use += 1
        return True

    def release(self) -> None:
        self.in_use = max(0, self.in_use - 1)
        if self._sem is not None:
            self._sem.release()


_limiter = _Limiter(config.MAX_CONCURRENT_COMPLETIONS, config.CONCURRENCY_WAIT)


async def _db_ready() -> None:
    """Schema + DB-backed background work. Runs at boot, or on reconnect."""
    await memory.ensure_schema()
    await cron.ensure_schema()
    if config.RUN_CHANNELS_IN_WEB:
        await channels.start_pollers()
    # The scheduler must run SOMEWHERE. With channels it lives in
    # channels_runner; an HTTP-only deployment has no such process, so jobs would
    # be created and never fire. Single worker only — see CRON_IN_WEB.
    if config.RUN_CHANNELS_IN_WEB or config.CRON_IN_WEB:
        if config.CRON_IN_WEB and config.WORKERS > 1:
            logger.warning("CRON_IN_WEB with WORKERS=%d: each worker runs its own "
                           "scheduler, so jobs will fire more than once. Use WORKERS=1 "
                           "or run the scheduler in a separate process.", config.WORKERS)
        await cron.start_scheduler()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The database is optional here: chat is stateless, so an outage must not
    # take the gateway down with it. Boot degraded and heal in the background.
    if await db.try_init_pool():
        await _db_ready()
    else:
        await db.start_reconnector(on_connect=_db_ready)
    yield
    await db.stop_reconnector()
    if config.RUN_CHANNELS_IN_WEB:
        await channels.stop_pollers()
    await db.close_pool()
    await httpclient.close_client()
    await redisc.close_redis()
    await agent.close()


app = FastAPI(title="proteus", version="0.1.0", lifespan=lifespan)

# Mount channel webhook routes (no-op if a channel isn't configured).
for _router in channels.routers():
    app.include_router(_router)


def _check_auth(authorization: str | None) -> None:
    if not config.API_KEY:
        return  # open mode (dev only)
    expected = f"Bearer {config.API_KEY}"
    if not secrets.compare_digest(authorization or "", expected):
        raise HTTPException(status_code=401, detail="invalid api key")


def _host_tools_allowed(admin_key: str | None) -> bool:
    """Host-access tools over HTTP require a SECOND, admin-only key.

    API_KEY is typically shared with every product surface that talks to the
    gateway, so it authenticates a surface, not an operator. Without
    ADMIN_API_KEY set (the default) no HTTP request can reach shell/run_code/
    email/schedule, regardless of profile."""
    if not config.ADMIN_API_KEY:
        return False
    return secrets.compare_digest(admin_key or "", config.ADMIN_API_KEY)


def _resolve_user_id(body: dict[str, Any], header_uid: str | None) -> str | None:
    if header_uid and header_uid.strip():
        return header_uid.strip()
    if isinstance(body.get("user"), str) and body["user"].strip():
        return body["user"].strip()
    # session_key "<agent>:<id>" lets a caller carry both in one field.
    sk = body.get("session_key")
    if isinstance(sk, str) and ":" in sk:
        return sk.split(":", 1)[1].strip() or None
    for m in body.get("messages", []):
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            match = _USER_RE.search(m["content"])
            if match:
                return match.group(1)
    return None


def _resolve_profile(body: dict[str, Any], header_profile: str | None) -> str | None:
    """Explicit X-Proteus-Profile / body.profile wins; else the session_key prefix,
    so a caller sending session_key='support:u123' selects the `support` agent."""
    if header_profile:
        return header_profile
    if body.get("profile"):
        return body["profile"]
    sk = body.get("session_key")
    if isinstance(sk, str) and ":" in sk:
        return sk.split(":", 1)[0].strip() or None
    return None


def _resolve_mode(body: dict[str, Any], header_mode: str | None) -> str | None:
    """Per-request behaviour block. X-Proteus-Mode header wins, else body.mode.
    Absent → nothing injected, which is what channels and plain callers get."""
    if header_mode and header_mode.strip():
        return header_mode.strip()
    m = body.get("mode")
    if isinstance(m, str) and m.strip():
        return m.strip()
    return None


def _chunk(chat_id: str, created: int, *, delta: dict | None = None,
           finish: str | None = None, tool_event: dict | None = None) -> str:
    payload: dict[str, Any] = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": config.MODEL,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if tool_event is not None:
        payload["proteus_tool_event"] = tool_event
    return f"data: {_dumps(payload)}\n\n"


async def _sse(user_id: str, body: dict[str, Any], profile: str | None = None,
               extra_system: str = "", host_tools: bool = False) -> AsyncGenerator[str, None]:
    chat_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    t0 = time.time()
    ttft: float | None = None
    tools_used = 0
    # The slot is held for the WHOLE stream, not just the first byte: an
    # in-flight stream is still consuming upstream capacity. `finally` releases
    # it even when the client disconnects mid-stream, which cancels this
    # generator — without that, every abandoned stream would leak a slot until
    # the process restarted.
    # Optional delta batching. One SSE frame per token is smoothest but means a
    # JSON encode and a write per token; at high concurrency those add up. A few
    # ms of coalescing cuts that sharply and is imperceptible. Off by default,
    # because trading smoothness for throughput should be a deliberate choice.
    coalesce = config.STREAM_COALESCE_MS / 1000.0
    pending: list[str] = []
    last_flush = time.time()

    def _flush() -> str | None:
        nonlocal pending, last_flush
        if not pending:
            return None
        text = "".join(pending)
        pending = []
        last_flush = time.time()
        return _chunk(chat_id, created, delta={"content": text})

    try:
        yield _chunk(chat_id, created, delta={"role": "assistant"})
        async for ev in agent.run(user_id, body.get("messages", []), extra_system=extra_system,
                                  profile=profile, host_tools=host_tools):
            if ev["type"] == "text":
                first_token = ttft is None
                if first_token:
                    ttft = time.time() - t0
                if not coalesce:
                    yield _chunk(chat_id, created, delta={"content": ev["text"]})
                else:
                    pending.append(ev["text"])
                    # The FIRST token always goes out immediately — TTFT is the
                    # latency users actually feel, and must never be buffered.
                    if first_token or (time.time() - last_flush) >= coalesce:
                        out = _flush()
                        if out:
                            yield out
            elif ev["type"] == "tool":
                tools_used += 1
                out = _flush()          # ordering: text before its tool event
                if out:
                    yield out
                yield _chunk(chat_id, created, tool_event=ev["event"])
            elif ev["type"] == "error":
                out = _flush()          # don't drop buffered text on the way out
                if out:
                    yield out
                yield _chunk(chat_id, created, delta={"content": f"\n\n⚠️ {ev['message']}"}, finish="stop")
                yield "data: [DONE]\n\n"
                return
            elif ev["type"] == "done":
                break
        out = _flush()                  # ...nor at the end of a clean stream
        if out:
            yield out
        yield _chunk(chat_id, created, finish="stop")
        yield "data: [DONE]\n\n"
    finally:
        _limiter.release()
        logger.info("stream user=%s profile=%s ttft=%s total=%.0fms tools=%d inflight=%d",
                    user_id, profile or config.DEFAULT_PROFILE,
                    f"{ttft*1000:.0f}ms" if ttft else "none",
                    (time.time() - t0) * 1000, tools_used, _limiter.in_use)


async def _collect(user_id: str, body: dict[str, Any], profile: str | None = None,
                   extra_system: str = "", host_tools: bool = False) -> JSONResponse:
    chat_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    t0 = time.time()
    text_parts: list[str] = []
    tool_events: list[dict] = []
    error: str | None = None
    try:
        async for ev in agent.run(user_id, body.get("messages", []), extra_system=extra_system,
                                  profile=profile, host_tools=host_tools):
            if ev["type"] == "text":
                text_parts.append(ev["text"])
            elif ev["type"] == "tool":
                tool_events.append(ev["event"])
            elif ev["type"] == "error":
                error = ev["message"]
                break
    finally:
        _limiter.release()
        logger.info("chat user=%s profile=%s total=%.0fms tools=%d inflight=%d",
                    user_id, profile or config.DEFAULT_PROFILE,
                    (time.time() - t0) * 1000, len(tool_events), _limiter.in_use)
    content = "".join(text_parts)
    if error and not content:
        content = f"⚠️ {error}"
    return JSONResponse({
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": config.MODEL,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "proteus_tool_events": tool_events,
    })


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_proteus_user_id: str | None = Header(default=None),
    x_proteus_profile: str | None = Header(default=None),
    x_proteus_mode: str | None = Header(default=None),
    x_proteus_admin_key: str | None = Header(default=None),
):
    _check_auth(authorization)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    user_id = _resolve_user_id(body, x_proteus_user_id)
    if not user_id:
        raise HTTPException(status_code=400, detail="missing user id (X-Proteus-User-Id header, 'user' field, or a 'user_id: <id>' system message)")

    # Profile selects persona + toolset (header / body.profile / session_key prefix).
    profile = _resolve_profile(body, x_proteus_profile)
    # A mode is a named behaviour block the AGENT defines; the gateway only
    # looks it up. Absent or unknown → nothing injected.
    _agent = profiles.pick(profile)
    extra_system = _agent.mode_block(_resolve_mode(body, x_proteus_mode)) if _agent else ""
    # Host-access tools are off for HTTP callers unless an admin key is presented.
    host_tools = _host_tools_allowed(x_proteus_admin_key)

    # Take a concurrency slot BEFORE starting work. Both _sse and _collect
    # release it in a `finally`, so every exit path (success, error, client
    # disconnect) returns the slot.
    if not await _limiter.acquire():
        logger.warning("shedding load: %d/%d slots busy, %d rejected so far",
                       _limiter.in_use, _limiter.limit, _limiter.rejected)
        raise HTTPException(
            status_code=503,
            detail="gateway at capacity, retry shortly",
            headers={"Retry-After": "5"},
        )

    if body.get("stream"):
        return StreamingResponse(
            _sse(user_id, body, profile, extra_system, host_tools),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
            },
        )
    return await _collect(user_id, body, profile, extra_system, host_tools)


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": config.MODEL, "object": "model", "owned_by": "proteus"}]}


@app.get("/healthz")
async def healthz():
    db_ok = await db.healthcheck()
    # Model auth is checked too — a dead provider token means every chat fails
    # while the DB is perfectly healthy, which used to read as "ok".
    auth: dict[str, Any] = {"source": "api-key"}
    if config.MODEL.startswith(("codex/", "openai-codex/")):
        from . import codex_auth

        auth = codex_auth.status()
    # Chat is stateless, so a dead database is degraded, not down — the gateway
    # still answers, without memory/channels/cron. Dead model auth IS down:
    # every completion would fail, so a load balancer should pull this instance.
    serving = bool(auth.get("ok", True))
    status = "ok" if (serving and db_ok) else ("degraded" if serving else "down")
    return JSONResponse(
        {
            "status": status,
            "db": db_ok,
            "model": config.MODEL,
            "model_auth": auth,
            "toolset": config.TOOLSET,
            "host_tools_over_http": bool(config.ADMIN_API_KEY),
            "channels": channels.enabled_channels(),
            # Saturation, per worker. This is the number to scale on: sustained
            # in_use near limit means add workers or replicas, and a climbing
            # `rejected` means you already needed to.
            "concurrency": {
                "in_use": _limiter.in_use,
                "limit": _limiter.limit,
                "rejected": _limiter.rejected,
            },
        },
        status_code=200 if serving else 503,
    )
