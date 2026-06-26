"""
Telegram channel — Bot API.

Two inbound modes:
  - polling  (default): a background getUpdates loop. No public URL/TLS needed —
    ideal for dev. Must run in exactly ONE process (see channels_runner).
  - webhook: Telegram POSTs updates to /channels/telegram/webhook.

Enabled when TELEGRAM_BOT_TOKEN is set.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Request

from .. import config
from ..httpclient import get_client
from .base import handle_inbound
from ._util import chunks, data_url, send_with_retry

logger = logging.getLogger("acag.channels.telegram")
router = APIRouter()

NAME = "telegram"


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"


async def _send(chat_id: str, text: str) -> None:
    # Telegram caps messages at 4096 chars — chunk on newline boundaries.
    for part in chunks(text, 4000):
        await send_with_retry(
            lambda p=part: get_client().post(_api("sendMessage"), json={"chat_id": chat_id, "text": p})
        )


async def _typing(chat_id: str) -> None:
    await get_client().post(_api("sendChatAction"), json={"chat_id": chat_id, "action": "typing"})


_EDIT_INTERVAL = 1.1  # seconds between edits (Telegram rate-limits edits)


class TelegramStreamer:
    """Live-streams a reply by editing one message as text accumulates.

    Sends the first message as soon as there's text, then throttled
    editMessageText calls; finish() writes the complete text (chunking >4096)."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.msg_id: int | None = None
        self.last_text = ""
        self.last_edit = 0.0

    async def update(self, text: str) -> None:
        disp = text[:4000]
        if not disp.strip():
            return
        if self.msg_id is None:
            r = await get_client().post(_api("sendMessage"), json={"chat_id": self.chat_id, "text": disp})
            self.msg_id = r.json().get("result", {}).get("message_id")
            self.last_text = disp
            self.last_edit = time.monotonic()
            return
        if time.monotonic() - self.last_edit < _EDIT_INTERVAL or disp == self.last_text:
            return
        try:
            await get_client().post(
                _api("editMessageText"),
                json={"chat_id": self.chat_id, "message_id": self.msg_id, "text": disp},
            )
            self.last_text = disp
            self.last_edit = time.monotonic()
        except Exception:
            pass  # ignore transient edit errors / "not modified"

    async def finish(self, text: str) -> None:
        parts = chunks(text or "…", 4000)
        if self.msg_id is None:
            for p in parts:
                await send_with_retry(lambda p=p: get_client().post(_api("sendMessage"), json={"chat_id": self.chat_id, "text": p}))
            return
        if parts[0] != self.last_text:
            try:
                await get_client().post(
                    _api("editMessageText"),
                    json={"chat_id": self.chat_id, "message_id": self.msg_id, "text": parts[0]},
                )
            except Exception:
                pass
        for p in parts[1:]:
            await send_with_retry(lambda p=p: get_client().post(_api("sendMessage"), json={"chat_id": self.chat_id, "text": p}))


def _sender_id(msg: dict) -> str:
    return str(msg.get("from", {}).get("id", ""))


def _allowed_match(msg: dict) -> bool:
    if not config.TELEGRAM_ALLOWED_USERS:
        return True
    frm = msg.get("from", {})
    return str(frm.get("id")) in config.TELEGRAM_ALLOWED_USERS or \
        (frm.get("username") or "") in config.TELEGRAM_ALLOWED_USERS


async def _download_file(file_id: str) -> bytes | None:
    """Resolve a Telegram file_id and download its bytes."""
    try:
        r = await get_client().post(_api("getFile"), json={"file_id": file_id})
        path = r.json().get("result", {}).get("file_path")
        if not path:
            return None
        url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{path}"
        img = await get_client().get(url)
        img.raise_for_status()
        return img.content
    except Exception:
        logger.warning("telegram file download failed for %s", file_id)
        return None


async def _extract_images(msg: dict) -> list[str]:
    """Collect image data-URLs from a Telegram message (photo or image document)."""
    out: list[str] = []
    if msg.get("photo"):
        largest = msg["photo"][-1]  # last entry = highest resolution
        raw = await _download_file(largest["file_id"])
        if raw:
            out.append(data_url(raw, "image/jpeg"))
    doc = msg.get("document")
    if doc and str(doc.get("mime_type", "")).startswith("image/"):
        raw = await _download_file(doc["file_id"])
        if raw:
            out.append(data_url(raw, doc["mime_type"]))
    return out


async def _process(msg: dict) -> None:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id or not _allowed_match(msg):
        return
    text = msg.get("text") or msg.get("caption") or ""
    images = await _extract_images(msg)
    if not text and not images:
        return  # unsupported message type (sticker, voice, etc.)
    dedup_id = f"{chat_id}:{msg.get('message_id', '')}"
    await handle_inbound(
        NAME,
        _sender_id(msg),
        text,
        send=lambda t: _send(chat_id, t),
        typing=lambda: _typing(chat_id),
        dedup_id=dedup_id,
        make_streamer=lambda: TelegramStreamer(chat_id),  # live message-edit streaming
        images=images,
    )


@router.post("/channels/telegram/webhook")
async def telegram_webhook(request: Request):
    if not enabled():
        return {"ok": False, "error": "telegram not configured"}
    update = await request.json()
    msg = update.get("message") or update.get("edited_message")
    if msg:
        await _process(msg)
    return {"ok": True}


async def poll() -> None:
    """Background long-poll loop. Run in a single process only."""
    logger.info("telegram poller starting")
    # Clear any previously-registered webhook, else getUpdates returns 409 Conflict.
    try:
        await get_client().post(_api("deleteWebhook"))
    except Exception as exc:
        logger.warning("telegram deleteWebhook failed: %s", exc)
    offset = 0
    while True:
        try:
            resp = await get_client().get(
                _api("getUpdates"),
                params={"offset": offset, "timeout": 50, "allowed_updates": '["message"]'},
                timeout=60,
            )
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message")
                if msg:
                    asyncio.create_task(_process(msg))
        except asyncio.CancelledError:
            logger.info("telegram poller stopped")
            raise
        except Exception as exc:
            logger.warning("telegram poll error: %s", exc)
            await asyncio.sleep(3)


async def setup_webhook() -> None:
    if config.PUBLIC_BASE_URL:
        url = f"{config.PUBLIC_BASE_URL}/channels/telegram/webhook"
        await get_client().post(_api("setWebhook"), json={"url": url, "allowed_updates": ["message"]})
        logger.info("telegram webhook set to %s", url)
