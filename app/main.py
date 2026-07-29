"""
acag HTTP surface — OpenAI-compatible so existing callers (qubi-staging, the
mobile chat route) switch over by changing only the base URL.

  POST /v1/chat/completions   {messages, stream?, user?}   → SSE or JSON
  GET  /healthz                                             → liveness + DB check
  GET  /v1/models                                           → advertises the active model

user_id (the per-student scope for every tool) is resolved, in priority order:
  1. X-Qubi-User-Id header        (preferred for new integrations)
  2. body "user" field            (OpenAI's standard field)
  3. "Student user_id: <id>" in a system message   (back-compat with Hermes callers)
The model never sees it — it is injected server-side at tool dispatch.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import agent, channels, config, cron, db, httpclient, memory, modes, redisc

_USER_RE = re.compile(r"user_id:\s*([^\s]+)", re.IGNORECASE)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.init_pool()
    await memory.ensure_schema()
    await cron.ensure_schema()
    if config.RUN_CHANNELS_IN_WEB:
        await channels.start_pollers()
        await cron.start_scheduler()
    yield
    if config.RUN_CHANNELS_IN_WEB:
        await channels.stop_pollers()
    await db.close_pool()
    await httpclient.close_client()
    await redisc.close_redis()
    await agent.close()


app = FastAPI(title="acag", version="0.1.0", lifespan=lifespan)

# Mount channel webhook routes (no-op if a channel isn't configured).
for _router in channels.routers():
    app.include_router(_router)


def _check_auth(authorization: str | None) -> None:
    if not config.API_KEY:
        return  # open mode (dev only)
    expected = f"Bearer {config.API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid api key")


def _resolve_user_id(body: dict[str, Any], header_uid: str | None) -> str | None:
    if header_uid and header_uid.strip():
        return header_uid.strip()
    if isinstance(body.get("user"), str) and body["user"].strip():
        return body["user"].strip()
    # session_key "qubi:<id>" (sent by the IntelliQ app) scopes to the student.
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
    """Explicit X-Acag-Profile / body.profile wins; else infer from session_key prefix
    (the IntelliQ app sends session_key='qubi:<id>')."""
    if header_profile:
        return header_profile
    if body.get("profile"):
        return body["profile"]
    sk = body.get("session_key")
    if isinstance(sk, str) and ":" in sk:
        return sk.split(":", 1)[0].strip() or None
    return None


def _resolve_mode(body: dict[str, Any], header_mode: str | None) -> str | None:
    """Study mode (simple/exam/teachback). X-Qubi-Mode header wins, else body.mode.
    Absent → no mode block injected (channels / legacy callers unchanged)."""
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
        payload["qubi_tool_event"] = tool_event
    return f"data: {json.dumps(payload)}\n\n"


async def _sse(user_id: str, body: dict[str, Any], profile: str | None = None,
               extra_system: str = "") -> AsyncGenerator[str, None]:
    chat_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    yield _chunk(chat_id, created, delta={"role": "assistant"})
    async for ev in agent.run(user_id, body.get("messages", []), extra_system=extra_system, profile=profile):
        if ev["type"] == "text":
            yield _chunk(chat_id, created, delta={"content": ev["text"]})
        elif ev["type"] == "tool":
            yield _chunk(chat_id, created, tool_event=ev["event"])
        elif ev["type"] == "error":
            yield _chunk(chat_id, created, delta={"content": f"\n\n⚠️ {ev['message']}"}, finish="stop")
            yield "data: [DONE]\n\n"
            return
        elif ev["type"] == "done":
            break
    yield _chunk(chat_id, created, finish="stop")
    yield "data: [DONE]\n\n"


async def _collect(user_id: str, body: dict[str, Any], profile: str | None = None,
                   extra_system: str = "") -> JSONResponse:
    chat_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    text_parts: list[str] = []
    tool_events: list[dict] = []
    error: str | None = None
    async for ev in agent.run(user_id, body.get("messages", []), extra_system=extra_system, profile=profile):
        if ev["type"] == "text":
            text_parts.append(ev["text"])
        elif ev["type"] == "tool":
            tool_events.append(ev["event"])
        elif ev["type"] == "error":
            error = ev["message"]
            break
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
        "qubi_tool_events": tool_events,
    })


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_qubi_user_id: str | None = Header(default=None),
    x_acag_profile: str | None = Header(default=None),
    x_qubi_mode: str | None = Header(default=None),
):
    _check_auth(authorization)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")

    user_id = _resolve_user_id(body, x_qubi_user_id)
    if not user_id:
        raise HTTPException(status_code=400, detail="missing user id (X-Qubi-User-Id header, 'user' field, or 'Student user_id:' system message)")

    # Profile selects persona + toolset (header / body.profile / session_key prefix).
    profile = _resolve_profile(body, x_acag_profile)
    # Study mode (simple/exam/teachback) is injected as an extra system block; absent → none.
    extra_system = modes.block_for(_resolve_mode(body, x_qubi_mode))

    if body.get("stream"):
        return StreamingResponse(
            _sse(user_id, body, profile, extra_system),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
            },
        )
    return await _collect(user_id, body, profile, extra_system)


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": config.MODEL, "object": "model", "owned_by": "acag"}]}


@app.get("/healthz")
async def healthz():
    ok = await db.healthcheck()
    return JSONResponse(
        {
            "status": "ok" if ok else "degraded",
            "db": ok,
            "model": config.MODEL,
            "toolset": config.TOOLSET,
            "channels": channels.enabled_channels(),
        },
        status_code=200 if ok else 503,
    )
