"""
WhatsApp channel — Meta WhatsApp Cloud API.

Webhook-only (Meta pushes events to a public HTTPS URL):
  GET  /channels/whatsapp/webhook  — verification handshake (hub.challenge)
  POST /channels/whatsapp/webhook  — inbound messages (HMAC-SHA256 verified)

Outbound via the Graph API. Enabled when WHATSAPP_ACCESS_TOKEN and
WHATSAPP_PHONE_NUMBER_ID are set.

Deployment: expose the route over TLS (nginx) and register the webhook URL +
verify token in the Meta app dashboard.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Request, Response

from .. import config
from ..httpclient import get_client
from .base import handle_inbound
from ._util import chunks, data_url, send_with_retry

logger = logging.getLogger("proteus.channels.whatsapp")
router = APIRouter()

NAME = "whatsapp"


def enabled() -> bool:
    return bool(config.WHATSAPP_ACCESS_TOKEN and config.WHATSAPP_PHONE_NUMBER_ID)


async def _send(to: str, text: str) -> None:
    url = (
        f"https://graph.facebook.com/{config.WHATSAPP_GRAPH_VERSION}/"
        f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    headers = {"Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}"}
    # WhatsApp text bodies cap at 4096 chars — chunk rather than truncate.
    for part in chunks(text, 4000):
        await send_with_retry(
            lambda p=part: get_client().post(
                url,
                headers=headers,
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": to,
                    "type": "text",
                    "text": {"body": p, "preview_url": True},
                },
            )
        )


def _verify_signature(body: bytes, signature: str | None) -> bool:
    if not config.WHATSAPP_APP_SECRET:
        return True  # verification disabled if no secret configured
    if not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(config.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature.split("=", 1)[1])


@router.get("/channels/whatsapp/webhook")
async def whatsapp_verify(request: Request):
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == config.WHATSAPP_VERIFY_TOKEN
    ):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    return Response(content="forbidden", status_code=403)


@router.post("/channels/whatsapp/webhook")
async def whatsapp_inbound(request: Request):
    raw = await request.body()
    if not _verify_signature(raw, request.headers.get("x-hub-signature-256")):
        return Response(content="bad signature", status_code=403)
    if not enabled():
        return {"ok": False}

    payload = await request.json()
    if payload.get("object") != "whatsapp_business_account":
        return {"ok": True}  # ignore non-WABA callbacks
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue  # skip status/template updates
            value = change.get("value", {})
            for msg in value.get("messages", []):
                mtype = msg.get("type")
                sender = msg.get("from")
                wamid = msg.get("id")  # dedup key — Meta retries on non-200/timeout
                text, images = "", []
                if mtype == "text":
                    text = msg.get("text", {}).get("body", "")
                elif mtype == "image":
                    img = msg.get("image", {})
                    text = img.get("caption", "")
                    media = await _download_media(img.get("id"))
                    if media:
                        images.append(data_url(media[0], media[1]))
                else:
                    continue
                if sender and (text or images):
                    await handle_inbound(
                        NAME, sender, text,
                        send=lambda t, s=sender: _send(s, t),
                        dedup_id=wamid,
                        images=images,
                    )
    return {"ok": True}


async def _download_media(media_id: str | None) -> tuple[bytes, str] | None:
    """Two-step WhatsApp media download: media_id → temp URL → bytes."""
    if not media_id:
        return None
    headers = {"Authorization": f"Bearer {config.WHATSAPP_ACCESS_TOKEN}"}
    try:
        meta = await get_client().get(
            f"https://graph.facebook.com/{config.WHATSAPP_GRAPH_VERSION}/{media_id}", headers=headers
        )
        info = meta.json()
        url = info.get("url")
        mime = info.get("mime_type", "image/jpeg")
        if not url:
            return None
        blob = await get_client().get(url, headers=headers)
        blob.raise_for_status()
        return blob.content, mime
    except Exception:
        logger.warning("whatsapp media download failed for %s", media_id)
        return None
