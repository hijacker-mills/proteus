"""
A task list the model keeps for itself, ported from the Hermes agent.

Why it earns its place: on a multi-step job a model will happily drift, redo a
finished step, or quietly drop one. Writing the plan down and ticking items off
turns that from a memory problem into a lookup.

ADAPTED FOR A STATELESS GATEWAY. Hermes keeps one store per session in memory,
which it can, because it is one long-lived process per conversation. Proteus is
several worker processes and any of them may serve the next turn, so an
in-memory list would appear to work in dev with WORKERS=1 and then silently lose
half its items in production. Todos are therefore keyed by user and persisted.

Without a database it degrades to per-process memory, which is fine for a single
worker and honestly reported as a caveat rather than pretended away.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .. import db

logger = logging.getLogger("proteus.tools.todo")

VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")
MAX_ITEMS = 256
MAX_CONTENT = 4000

# Fallback when there is no database: correct for one worker, lossy for several.
_fallback: dict[str, list[dict]] = {}


def _validate(raw: Any, index: int) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    content = str(raw.get("content") or "").strip()[:MAX_CONTENT]
    if not content:
        return None
    status = str(raw.get("status") or "pending").strip().lower()
    return {
        "id": str(raw.get("id") or f"t{index + 1}").strip(),
        "content": content,
        "status": status if status in VALID_STATUSES else "pending",
    }


def _partial(raw: Any) -> dict[str, str] | None:
    """A merge entry: an id plus whichever fields the model chose to change.

    Merge exists precisely so the model can tick one item off with
    `{"id": "t2", "status": "completed"}`. Requiring `content` here would drop
    exactly those updates, and silently — the call succeeds and changes nothing.
    """
    if not isinstance(raw, dict):
        return None
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        return None
    out = {"id": item_id}
    content = str(raw.get("content") or "").strip()[:MAX_CONTENT]
    if content:
        out["content"] = content
    status = str(raw.get("status") or "").strip().lower()
    if status in VALID_STATUSES:
        out["status"] = status
    return out


def _clean(todos: Any, *, partial: bool = False) -> list[dict[str, str]]:
    """Validate, drop junk, and keep the LAST entry for a repeated id."""
    out: dict[str, dict[str, str]] = {}
    for i, raw in enumerate(todos or []):
        item = _partial(raw) if partial else _validate(raw, i)
        if item:
            out[item["id"]] = item
    return list(out.values())[:MAX_ITEMS]


async def _load(user_id: str) -> list[dict[str, str]]:
    if not db.available():
        return list(_fallback.get(user_id, []))
    try:
        async with db.get_pool().acquire() as c:
            row = await c.fetchval(
                "SELECT items FROM proteus.proteus_todo WHERE user_key=$1", user_id)
        return json.loads(row) if row else []
    except Exception:
        logger.exception("could not load todos for %s", user_id)
        return []


async def _save(user_id: str, items: list[dict[str, str]]) -> None:
    if not db.available():
        _fallback[user_id] = items
        return
    async with db.get_pool().acquire() as c:
        await c.execute(
            """INSERT INTO proteus.proteus_todo (user_key, items, updated_at)
               VALUES ($1, $2, now())
               ON CONFLICT (user_key) DO UPDATE
                 SET items = EXCLUDED.items, updated_at = now()""",
            user_id, json.dumps(items))


def _summary(items: list[dict[str, str]]) -> dict[str, int]:
    counts = {s: 0 for s in VALID_STATUSES}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    return {"total": len(items), **counts}


async def todo(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    """Read the list, or write it. `merge` updates by id instead of replacing."""
    todos = args.get("todos")

    if todos is None:                       # read
        items = await _load(user_id)
        return {"todos": items, "summary": _summary(items)}

    if args.get("merge"):
        existing = {i["id"]: i for i in await _load(user_id)}
        for item in _clean(todos, partial=True):
            if item["id"] in existing:
                existing[item["id"]].update(item)          # only the given fields
            elif "content" in item:
                existing[item["id"]] = {"status": "pending", **item}
            # a partial update naming an unknown id, with no content, is ignored
        items = list(existing.values())[:MAX_ITEMS]
    else:
        items = _clean(todos)

    try:
        await _save(user_id, items)
    except Exception:
        logger.exception("could not save todos for %s", user_id)
        return {"error": "could not save the task list"}
    return {"todos": items, "summary": _summary(items)}


SCHEMA = {
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "Track a multi-step task. Call with no arguments to read the current list. "
            "Call with `todos` to write it. Use this whenever a job has more than two "
            "steps: write the plan first, then mark each item in_progress and completed "
            "as you go, so nothing is repeated or forgotten."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The full list. Omit entirely to read instead of write.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Stable id, e.g. t1"},
                            "content": {"type": "string", "description": "What the step is"},
                            "status": {"type": "string", "enum": list(VALID_STATUSES)},
                        },
                        "required": ["content"],
                    },
                },
                "merge": {
                    "type": "boolean",
                    "description": "Update matching ids and append new ones, rather than "
                                   "replacing the whole list. Use when ticking one item off.",
                },
            },
            "required": [],
        },
    },
}
