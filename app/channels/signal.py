"""
Signal channel — via signal-cli-rest-api (bbernhard/signal-cli-rest-api).

Signal has no official bot API; the standard approach is a local signal-cli
daemon fronted by a REST wrapper. acag polls it for inbound messages and posts
replies back.

  receive: GET  {SIGNAL_CLI_REST_URL}/v1/receive/{SIGNAL_NUMBER}
  send:    POST {SIGNAL_CLI_REST_URL}/v2/send

Enabled when SIGNAL_CLI_REST_URL and SIGNAL_NUMBER are set. Setup: run the
signal-cli-rest-api container and register/link SIGNAL_NUMBER once. Polling must
run in a single process (see channels_runner).
"""
from __future__ import annotations

import asyncio
import logging

from .. import config
from ..httpclient import get_client
from .base import handle_inbound
from ._util import data_url, send_with_retry

logger = logging.getLogger("acag.channels.signal")

NAME = "signal"


def enabled() -> bool:
    return bool(config.SIGNAL_CLI_REST_URL and config.SIGNAL_NUMBER)


async def _send(recipient: str, text: str) -> None:
    await send_with_retry(
        lambda: get_client().post(
            f"{config.SIGNAL_CLI_REST_URL}/v2/send",
            json={
                "message": text,
                "number": config.SIGNAL_NUMBER,
                "recipients": [recipient],
            },
        )
    )


async def _download_attachment(att_id: str, mime: str) -> str | None:
    try:
        r = await get_client().get(f"{config.SIGNAL_CLI_REST_URL}/v1/attachments/{att_id}")
        r.raise_for_status()
        return data_url(r.content, mime or "image/jpeg")
    except Exception:
        logger.warning("signal attachment download failed for %s", att_id)
        return None


async def _process(envelope: dict) -> None:
    env = envelope.get("envelope", envelope)
    source = env.get("source") or env.get("sourceNumber") or env.get("sourceUuid")
    data_msg = env.get("dataMessage") or {}
    text = data_msg.get("message") or ""

    images: list[str] = []
    for att in data_msg.get("attachments") or []:
        if str(att.get("contentType", "")).startswith("image/"):
            url = await _download_attachment(att.get("id"), att.get("contentType"))
            if url:
                images.append(url)

    # Ignore receipts / typing / sync envelopes (no dataMessage content).
    if source and (text or images):
        dedup_id = str(data_msg.get("timestamp") or env.get("timestamp") or "")
        await handle_inbound(
            NAME, source, text,
            send=lambda t, s=source: _send(s, t),
            dedup_id=f"{source}:{dedup_id}" if dedup_id else None,
            images=images,
        )


async def poll() -> None:
    """Background receive loop. Run in a single process only."""
    logger.info("signal poller starting (%s)", config.SIGNAL_NUMBER)
    url = f"{config.SIGNAL_CLI_REST_URL}/v1/receive/{config.SIGNAL_NUMBER}"
    while True:
        try:
            resp = await get_client().get(url, timeout=60)
            for envelope in resp.json() or []:
                asyncio.create_task(_process(envelope))
        except asyncio.CancelledError:
            logger.info("signal poller stopped")
            raise
        except Exception as exc:
            logger.warning("signal poll error: %s", exc)
        await asyncio.sleep(config.SIGNAL_POLL_INTERVAL)
