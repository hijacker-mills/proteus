"""
Channel registry.

- `routers()`    → APIRouters to mount on the web app (webhook endpoints). Always
                   mounted; each handler no-ops if its channel isn't configured.
- `start_pollers()` / `stop_pollers()` → background receive loops (Telegram
                   polling, Signal). These MUST run in a single process — use the
                   dedicated `app.channels_runner`, or RUN_CHANNELS_IN_WEB=true
                   with WORKERS=1 for dev.
"""
from __future__ import annotations

import asyncio
import logging

from . import signal as signal_ch
from . import telegram, whatsapp

logger = logging.getLogger("proteus.channels")

_tasks: list[asyncio.Task] = []


async def deliver(channel: str, target: str, text: str) -> None:
    """Send a message to a channel recipient (used by cron / proactive delivery)."""
    if channel == "telegram":
        await telegram._send(target, text)
    elif channel == "whatsapp":
        await whatsapp._send(target, text)
    elif channel == "signal":
        await signal_ch._send(target, text)
    else:
        logger.warning("deliver: unknown channel %s", channel)


def is_trusted(channel: str, sender: str) -> bool:
    """True only when the channel enforces an explicit allowlist AND this sender
    is on it — the trust boundary for host-access tools (see toolsets.HOST_TOOLS).

    Evaluated fresh at use time, so removing someone from TELEGRAM_ALLOWED_USERS
    also strips the privileges of jobs they scheduled earlier. Channels with no
    allowlist config (WhatsApp, Signal) are never trusted.
    """
    from .. import config

    if channel == "telegram":
        return bool(config.TELEGRAM_ALLOWED_USERS) and sender in config.TELEGRAM_ALLOWED_USERS
    return False


def enabled_channels() -> list[str]:
    names = []
    if telegram.enabled():
        names.append(f"telegram({telegram.config.TELEGRAM_MODE})")
    if whatsapp.enabled():
        names.append("whatsapp")
    if signal_ch.enabled():
        names.append("signal")
    return names


def routers():
    """Webhook routers — always mounted; gated internally by each channel."""
    return [telegram.router, whatsapp.router]


async def start_pollers() -> None:
    """Spawn polling-based channel loops. Call exactly once, in one process."""
    if telegram.enabled() and telegram.config.TELEGRAM_MODE == "polling":
        _tasks.append(asyncio.create_task(telegram.poll(), name="telegram-poll"))
    elif telegram.enabled() and telegram.config.TELEGRAM_MODE == "webhook":
        await telegram.setup_webhook()

    if signal_ch.enabled():
        _tasks.append(asyncio.create_task(signal_ch.poll(), name="signal-poll"))

    if _tasks:
        logger.info("started %d channel poller(s): %s", len(_tasks), [t.get_name() for t in _tasks])


async def stop_pollers() -> None:
    for task in _tasks:
        task.cancel()
    for task in _tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _tasks.clear()
