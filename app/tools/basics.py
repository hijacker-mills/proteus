"""
Small generic tools that most agents end up needing.

Each one exists because a language model is unreliable at it and a two-line
function is not:

  datetime    models do not know what time it is, and date arithmetic across
              timezones is a classic silent-wrong-answer.
  calculate   arithmetic is generated token by token, so a long expression is a
              plausible-looking guess. This evaluates it.
  remember /  per-user notes that survive the conversation, stored in proteus's
  recall      own table and scoped to the gateway-resolved user.

None of them touch the host, so none is a HOST_TOOL.
"""
from __future__ import annotations

import ast
import operator
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ── datetime ─────────────────────────────────────────────────────────────────

async def now(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    tz_name = str(args.get("timezone") or "UTC").strip()
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return {"error": f"unknown timezone {tz_name!r} (use an IANA name like Europe/London)"}

    moment = datetime.now(tz)
    shift = args.get("shift_days")
    if shift is not None:
        try:
            moment += timedelta(days=float(shift))
        except (TypeError, ValueError):
            return {"error": "shift_days must be a number"}

    return {
        "iso": moment.isoformat(),
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M:%S"),
        "weekday": moment.strftime("%A"),
        "timezone": tz_name,
        "utc_offset": moment.strftime("%z"),
        "unix": int(moment.timestamp()),
    }


# ── calculator ───────────────────────────────────────────────────────────────
# Evaluated over a whitelisted AST rather than with eval(): eval on model output
# is arbitrary code execution, and this tool is deliberately NOT a host tool.

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_MAX_POW = 1_000_000          # 9**9**9 would otherwise hang the worker


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numbers are allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) ** abs(right) > _MAX_POW):
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


async def calculate(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        return {"error": "expression is required"}
    if len(expression) > 500:
        return {"error": "expression too long"}
    try:
        value = _eval(ast.parse(expression, mode="eval").body)
    except ZeroDivisionError:
        return {"error": "division by zero"}
    except Exception as exc:
        return {"error": f"could not evaluate: {exc}"}
    return {"expression": expression, "result": value}


# ── per-user notes ───────────────────────────────────────────────────────────
# Backed by proteus's own memory table, so this needs no extra schema. `user_id`
# comes from the authenticated request, never from the model, which is what
# keeps one user's notes out of another's recall.

async def remember(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    from .. import db
    from ..memory import embed, store

    text = str(args.get("text") or "").strip()
    if not text:
        return {"error": "text is required"}
    if not db.available():
        return {"error": "memory is unavailable — this deployment has no database"}
    try:
        vector = await embed.embed(text)
        await store.add_memory(f"user:{user_id}", "note", text, vector)
    except Exception as exc:
        return {"error": f"could not store: {type(exc).__name__}"}
    return {"ok": True, "stored": text[:120]}


async def recall(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    from .. import config, db
    from ..memory import embed, store

    query = str(args.get("query") or "").strip()
    limit = max(1, min(int(args.get("limit") or 5), 20))
    if not query:
        return {"error": "query is required"}
    if not db.available():
        return {"error": "memory is unavailable — this deployment has no database"}
    try:
        vector = await embed.embed(query)
        hits = await store.recall(f"user:{user_id}", vector, limit, config.MEMORY_RECALL_MIN_SCORE)
    except Exception as exc:
        return {"error": f"could not search: {type(exc).__name__}"}
    return {"query": query,
            "memories": [{"text": h["text"], "score": round(float(h["score"]), 3)} for h in hits]}


TOOLS = [
    {"type": "function", "function": {
        "name": "datetime_now",
        "description": "The current date and time, in any IANA timezone, optionally shifted by "
                       "a number of days. Use this whenever the answer depends on today's date; "
                       "do not guess it.",
        "parameters": {"type": "object", "properties": {
            "timezone": {"type": "string", "description": "IANA name, e.g. Europe/London. Defaults to UTC."},
            "shift_days": {"type": "number", "description": "Offset in days; negative for the past."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "calculate",
        "description": "Evaluate an arithmetic expression exactly (+ - * / // % **). "
                       "Use this instead of doing multi-step arithmetic yourself.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "e.g. (1234 * 5678) / 3"},
        }, "required": ["expression"]}}},
    {"type": "function", "function": {
        "name": "remember",
        "description": "Save a durable note about this user, retrievable in later conversations.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "The fact to remember, as a full sentence."},
        }, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "recall",
        "description": "Search notes previously saved about this user with `remember`.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for."},
            "limit": {"type": "integer", "description": "Max results (default 5)."},
        }, "required": ["query"]}}},
]

from .todo import SCHEMA as _TODO_SCHEMA, todo as _todo   # noqa: E402

TOOLS.append(_TODO_SCHEMA)

_HANDLERS = {"datetime_now": now, "calculate": calculate, "remember": remember,
             "recall": recall, "todo": _todo}


async def dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}
    return await handler(user_id, args or {})
