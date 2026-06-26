"""
Tiered agent memory on Postgres + pgvector (the Redis alternative).

  working memory  → durable conversation log, fetched by recency
  long-term memory → distilled facts/preferences, fetched by semantic similarity

Per turn, `prepare()` assembles context = recent messages + an injected block of
the most relevant long-term memories. After the reply, `record()` logs both
messages and (every few turns) schedules background distillation.

Keyed by `user_key` (currently "channel:sender"); a future identity layer can map
several keys to one canonical person without touching this API.
"""
from __future__ import annotations

import asyncio
import logging

from .. import config, llm
from . import curator, embed, store

logger = logging.getLogger("acag.memory")

ensure_schema = store.ensure_schema
clear = store.clear_messages

_CAPTION_SYS = "You write concise, factual descriptions of images for an AI assistant's long-term memory."
_CAPTION_INSTR = (
    "Describe the image in 1-3 short sentences: key objects, people, any visible text, the scene, "
    "and anything personally relevant about the user. Just the description — no preamble."
)


async def caption_images(images: list[str], user_text: str = "") -> str:
    """Vision-caption inbound images so they can be stored as text memory."""
    if not images:
        return ""
    parts: list[dict] = [{"type": "text", "text": _CAPTION_INSTR + (f"\nThe user said: {user_text}" if user_text else "")}]
    for url in images:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    out = ""
    try:
        async for ev in llm.astream(
            model=config.MODEL,
            messages=[{"role": "system", "content": _CAPTION_SYS}, {"role": "user", "content": parts}],
            tools=None,
            max_tokens=200,
            temperature=0.2,
            reasoning_effort=config.MEMORY_CURATOR_EFFORT,
        ):
            if ev["type"] == "text":
                out += ev["text"]
    except Exception:
        logger.exception("image captioning failed")
    return out.strip()


async def prepare(user_key: str, text: str, content: object = None) -> tuple[list[dict], str]:
    """Return (messages, extra_system) for agent.run().

    `text` is the plain-text used for semantic recall (the caption, for image turns).
    `content` is what actually goes in the user message — a string, or OpenAI
    multimodal parts (text + image_url) for vision turns. Defaults to `text`.
    """
    turn = content if content is not None else text
    if not config.MEMORY_ENABLED:
        return [{"role": "user", "content": turn}], ""

    recent = await store.recent_messages(user_key, config.MEMORY_RECENT_MESSAGES)

    extra = ""
    if text and text.strip():
        try:
            qv = await embed.embed(text)
            hits = await store.recall(user_key, qv, config.MEMORY_RECALL_K, config.MEMORY_RECALL_MIN_SCORE)
            if hits:
                lines = "\n".join(f"- {h['text']}" for h in hits)
                extra = (
                    "Long-term memory about this person (use naturally; do not recite verbatim):\n"
                    + lines
                )
        except Exception:
            logger.exception("recall failed for %s", user_key)

    messages = recent + [{"role": "user", "content": turn}]
    return messages, extra


async def record(user_key: str, channel: str, user_text: str, assistant_text: str) -> None:
    """Persist the exchange; schedule distillation every MEMORY_DISTILL_EVERY turns."""
    if not config.MEMORY_ENABLED:
        return
    await store.add_message(user_key, channel, "user", user_text)
    await store.add_message(user_key, channel, "assistant", assistant_text)
    try:
        n = await store.user_turn_count(user_key)
        if config.MEMORY_DISTILL_EVERY > 0 and n % config.MEMORY_DISTILL_EVERY == 0:
            asyncio.create_task(_safe_curate(user_key))
    except Exception:
        logger.exception("curate scheduling failed for %s", user_key)


async def _safe_curate(user_key: str) -> None:
    try:
        await curator.curate(user_key)
    except Exception:
        logger.exception("curation failed for %s", user_key)
