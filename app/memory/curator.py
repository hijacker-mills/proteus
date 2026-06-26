"""
Memory curator — reconciles long-term memory after a few turns.

Unlike a blind extractor (which only ever ADDs), the curator sees the user's
CURRENT memories plus the recent conversation and emits a small set of
operations: add / update / remove. This gives, in one pass:
  - selectivity  — explicit SAVE/SKIP rules (adapted from Hermes); only durable,
                   high-value facts are kept, trivia/one-off tasks are skipped
  - overwrite    — when a fact changes or is corrected, the existing memory is
                   UPDATED in place (referenced by id), not duplicated
  - supersede    — contradicted/obsolete memories are REMOVED
  - bounded size — a soft cap forces consolidation instead of unbounded growth

Runs in the background (fire-and-forget); model-agnostic via llm.astream.
"""
from __future__ import annotations

import json
import logging

from .. import config, llm
from . import embed, store

logger = logging.getLogger("acag.memory.curator")

_PROMPT = (
    "You curate an AI assistant's long-term memory of ONE user. Keep it small, "
    "high-value, and current.\n\n"
    "SAVE (durable, reusable across future conversations):\n"
    "- the user's name and personal/identity facts\n"
    "- stable preferences (tone, format, language, how they want the assistant to behave)\n"
    '- standing instructions or a name the user gives the assistant (e.g. "you are Jack")\n'
    "- ongoing goals, projects, and recurring interests the user works on\n"
    "- constraints (allergies, tools/tech they rely on, things to avoid)\n\n"
    "SKIP (do NOT save):\n"
    "- trivial or obvious info, and facts easily re-derived\n"
    "- one-off questions and transient task details (e.g. a single coding request)\n"
    "- session-specific ephemera; anything about the assistant's replies\n"
    "- anything already captured by an existing memory (don't duplicate)\n\n"
    "RECONCILE against the CURRENT MEMORIES (each shown as [id] text):\n"
    "- add   — a genuinely new durable fact not already present\n"
    "- update— when the conversation CHANGES or CORRECTS an existing memory, rewrite "
    "that entry (reference its id). Newer info wins.\n"
    "- remove— when an existing memory is now contradicted or obsolete (reference its id)\n"
    f"Keep the total under ~{config.MEMORY_MAX_PER_USER} entries; if over, merge/remove "
    "the least valuable.\n\n"
    'Return ONLY JSON: {"ops":[{"op":"add","text":"..."},'
    '{"op":"update","id":12,"text":"..."},{"op":"remove","id":7}]}. '
    'Use {"ops":[]} if nothing should change. No prose, no markdown.'
)


def _parse_ops(s: str) -> list[dict]:
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s.split("\n", 1)[1] if "\n" in s else s
    a, b = s.find("{"), s.rfind("}")
    if a == -1 or b == -1:
        return []
    try:
        obj = json.loads(s[a:b + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    ops = obj.get("ops", [])
    return ops if isinstance(ops, list) else []


async def curate(user_key: str) -> dict[str, int]:
    msgs = await store.recent_messages(user_key, config.MEMORY_DISTILL_WINDOW)
    if not msgs:
        return {}
    existing = await store.list_memories(user_key, config.MEMORY_MAX_PER_USER * 2)

    existing_block = "\n".join(f"[{m['id']}] {m['text']}" for m in existing) or "(none yet)"
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in msgs)
    valid_ids = {m["id"] for m in existing}

    out = ""
    async for ev in llm.astream(
        model=config.MEMORY_CURATOR_MODEL,
        messages=[
            {"role": "system", "content": _PROMPT},
            {"role": "user", "content": f"CURRENT MEMORIES:\n{existing_block}\n\nRECENT CONVERSATION:\n{convo}\n\nReturn the operations JSON."},
        ],
        tools=None,
        max_tokens=500,
        temperature=0.0,
        reasoning_effort=config.MEMORY_CURATOR_EFFORT,
    ):
        if ev["type"] == "text":
            out += ev["text"]

    counts = {"add": 0, "update": 0, "remove": 0}
    for op in _parse_ops(out):
        kind_op = op.get("op")
        try:
            if kind_op == "remove" and op.get("id") in valid_ids:
                if await store.remove_memory(int(op["id"]), user_key):
                    counts["remove"] += 1
            elif kind_op == "update" and op.get("id") in valid_ids and op.get("text"):
                vec = await embed.embed(op["text"].strip())
                if await store.update_memory(int(op["id"]), user_key, op["text"].strip(), vec):
                    counts["update"] += 1
            elif kind_op == "add" and op.get("text"):
                text = op["text"].strip()
                vec = await embed.embed(text)
                top = await store.nearest_score(user_key, vec)
                if top is not None and top >= config.MEMORY_DEDUP_SIM:
                    continue  # near-duplicate already present
                await store.add_memory(user_key, op.get("kind", "fact"), text, vec)
                counts["add"] += 1
        except Exception:
            logger.exception("curate op failed: %s", op)

    trimmed = await store.trim(user_key, config.MEMORY_MAX_PER_USER)
    if any(counts.values()) or trimmed:
        logger.info("curated %s (trimmed %d) for %s", counts, trimmed, user_key)
    return counts
