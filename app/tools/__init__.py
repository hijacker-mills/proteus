"""Tool dispatch table. All handlers: async (user_id, args) -> dict."""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from .. import config
from . import memory, proxy

Handler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

DISPATCH: dict[str, Handler] = {
    "lecture_evidence": proxy.lecture_evidence,
    "exam_preferences": proxy.exam_preferences,
    "quiz_generate": proxy.quiz_generate,
    "recall_grade": proxy.recall_grade,
    "image_search": proxy.image_search,
    "memory_read": memory.memory_read,
    "memory_write": memory.memory_write,
    "memory_search": memory.memory_search,
}


async def dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = DISPATCH.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}
    # Map a channel identity (e.g. "telegram:123") to a real IntelliQ student id
    # so the study tools operate on that account's data.
    user_id = config.INTELLIQ_USER_MAP.get(user_id, user_id)
    return await handler(user_id, args or {})
