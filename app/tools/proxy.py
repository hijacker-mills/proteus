"""
Proxy tools — thin async HTTP wrappers around IntelliQ's /api/internal/qubi/*
routes. Auth contract: Bearer QUBI_INTERNAL_SECRET + x-user-id header
(identical to src/lib/qubi-internal-auth.ts in the web app).

The gateway supplies user_id; the model only provides topical arguments.
"""
from __future__ import annotations

from typing import Any

import httpx

from .. import config
from ..httpclient import get_client


async def _post(path: str, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
    if not config.QUBI_INTERNAL_SECRET:
        return {"error": "QUBI_INTERNAL_SECRET not configured"}
    try:
        resp = await get_client().post(
            f"{config.INTELLIQ_APP_URL}{path}",
            json=body,
            headers={
                "Authorization": f"Bearer {config.QUBI_INTERNAL_SECRET}",
                "x-user-id": user_id,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        try:
            return {"error": exc.response.json()}
        except Exception:
            return {"error": f"HTTP {exc.response.status_code}"}
    except Exception as exc:
        return {"error": str(exc)}


async def lecture_evidence(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "query": args.get("query", ""),
        "scope": args.get("scope", "library"),
        "limit": int(args.get("limit", 5)),
    }
    if args.get("lecture_id"):
        body["lectureId"] = args["lecture_id"]
    if args.get("course_id"):
        body["courseId"] = args["course_id"]
    return await _post("/api/internal/qubi/lecture_evidence", user_id, body)


async def exam_preferences(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return await _post(
        "/api/internal/qubi/exam_preferences",
        user_id,
        {"action": args.get("action", "get")},
    )


async def quiz_generate(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "count": int(args.get("count", 5)),
        "difficulty": args.get("difficulty", "medium"),
    }
    if args.get("note_id"):
        body["noteId"] = args["note_id"]
    if args.get("lecture_id"):
        body["lectureId"] = args["lecture_id"]
    return await _post("/api/internal/qubi/quiz_generate", user_id, body)


async def recall_grade(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return await _post(
        "/api/internal/qubi/recall_grade",
        user_id,
        {"noteId": args.get("note_id"), "attempt": args.get("attempt", "")},
    )


async def image_search(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    if not config.IMAGE_SEARCH_SERVICE_URL:
        return {"error": "image_search_not_configured"}
    try:
        resp = await get_client().post(
            f"{config.IMAGE_SEARCH_SERVICE_URL}/search",
            json={"query": args.get("query", ""), "count": int(args.get("count", 4))},
            headers={"Authorization": f"Bearer {config.IMAGE_SEARCH_SERVICE_TOKEN}"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return {"error": str(exc)}
