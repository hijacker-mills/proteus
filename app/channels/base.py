"""
Channel framework core.

Every channel (Telegram, WhatsApp, Signal, …) does the same thing on an inbound
message: load the sender's history, run the agent, save, and reply. That shared
flow lives in `handle_inbound`; each channel only supplies a `send` coroutine
(and optionally a `typing` indicator).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .. import agent, config, memory
from ._util import already_processed, session_lock

logger = logging.getLogger("acag.channels")

# A bound "send this text back to the sender" coroutine.
Send = Callable[[str], Awaitable[None]]
# An optional "show typing" coroutine.
Typing = Callable[[], Awaitable[None]] | None
# An optional live-streamer factory. The returned object must expose
#   async update(full_text) — render progress (throttled by the channel)
#   async finish(full_text) — render the final message
# Channels that can't edit messages (WhatsApp, Signal) pass None and get a single
# send at the end instead.
MakeStreamer = Callable[[], Any] | None


async def handle_inbound(
    channel: str,
    sender: str,
    text: str,
    send: Send,
    typing: Typing = None,
    allowed: list[str] | None = None,
    dedup_id: str | None = None,
    make_streamer: MakeStreamer = None,
    images: list[str] | None = None,
) -> None:
    """Run one inbound message end-to-end: history → agent → reply.

    - dedup_id: platform message id; if we've already processed it (webhook
      retry / redelivery), this is a no-op.
    - Messages for the same sender are serialized via a per-session lock, so a
      burst of quick messages can't race on the same conversation history.
    """
    text = (text or "").strip()
    if not sender or (not text and not images):
        return
    if allowed and sender not in allowed:
        logger.info("channel=%s rejected sender=%s (not in allowlist)", channel, sender)
        return
    if dedup_id and await already_processed(f"{channel}:{dedup_id}"):
        logger.debug("channel=%s duplicate dedup_id=%s — skipping", channel, dedup_id)
        return

    key = f"{channel}:{sender}"

    # Lightweight slash commands.
    if text.lower() in ("/reset", "/clear", "/new"):
        await memory.clear(key)
        await send("🧹 Conversation reset. (Long-term memory kept.)")
        return

    # Build the turn: multimodal content for the model, plain text for memory/recall.
    if images:
        content: object = ([{"type": "text", "text": text}] if text else []) + [
            {"type": "image_url", "image_url": {"url": u}} for u in images
        ]
        recall_text = text or "image"
    else:
        content = text
        recall_text = text

    # Caption images in parallel with the reply (persistent image memory) so it
    # adds no latency — awaited just before we persist the turn.
    caption_task = (
        asyncio.create_task(memory.caption_images(images, text))
        if images and config.MEMORY_VISION_CAPTION else None
    )

    logger.info("channel=%s in  sender=%s: %s%s", channel, sender, text[:60], f" [+{len(images)} img]" if images else "")

    streamer = make_streamer() if make_streamer else None

    async with session_lock(key):
        # context = recent working memory + semantically-recalled long-term memory
        messages, extra_system = await memory.prepare(key, recall_text, content=content)

        if typing:
            try:
                await typing()
            except Exception:
                pass

        acc = ""
        try:
            async for ev in agent.run(user_id=key, messages=messages, extra_system=extra_system):
                if ev["type"] == "text":
                    acc += ev["text"]
                    if streamer:
                        try:
                            await streamer.update(acc)  # throttled inside the channel
                        except Exception:
                            pass
                elif ev["type"] == "tool" and streamer:
                    try:  # transient "using a tool" indicator, cleared by next text
                        await streamer.update((acc + f"\n\n🔧 _{ev['event'].get('tool', 'tool')}…_").strip())
                    except Exception:
                        pass
                elif ev["type"] == "error":
                    acc += f"\n\n⚠️ {ev['message']}"
        except Exception as exc:  # never let a single message kill the poller/route
            logger.exception("channel=%s agent error", channel)
            acc += f"\n\n⚠️ {exc}"

        reply = acc.strip() or "…"

        # Persist the turn — for images, store the caption so it's recallable later.
        if images:
            desc = ""
            if caption_task:
                try:
                    desc = await caption_task
                except Exception:
                    pass
            mem_text = (text + " " if text else "") + (
                f"[image: {desc}]" if desc else f"[sent {len(images)} image{'s' if len(images) > 1 else ''}]"
            )
        else:
            mem_text = text
        await memory.record(key, channel, mem_text, reply)

    try:
        if streamer:
            await streamer.finish(reply)
        else:
            await send(reply)
        logger.info("channel=%s out sender=%s: %d chars%s", channel, sender, len(reply), " (streamed)" if streamer else "")
    except Exception:
        logger.exception("channel=%s send failed sender=%s", channel, sender)
