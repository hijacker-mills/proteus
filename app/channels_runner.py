"""
Standalone channel-poller process.

Runs the polling-based channels (Telegram long-poll, Signal receive) in a SINGLE
process — separate from the uvicorn web workers, which would otherwise each spawn
a competing poller. Webhook channels (WhatsApp, Telegram webhook mode) are served
by the web app and don't need this.

Run:  python -m app.channels_runner   (or scripts/run_channels.sh / PM2)
"""
from __future__ import annotations

import asyncio
import logging
import signal as signalmod

from . import channels, cron, db, httpclient, memory, redisc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("acag.channels_runner")


async def main() -> None:
    await db.init_pool()
    await memory.ensure_schema()
    await cron.ensure_schema()
    await cron.start_scheduler()
    enabled = channels.enabled_channels()
    if not enabled:
        logger.warning("no channels configured — set TELEGRAM_BOT_TOKEN / SIGNAL_* and restart")
    else:
        logger.info("channels enabled: %s", enabled)
    await channels.start_pollers()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signalmod.SIGINT, signalmod.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()
    logger.info("shutting down…")
    await cron.stop_scheduler()
    await channels.stop_pollers()
    await db.close_pool()
    await httpclient.close_client()
    await redisc.close_redis()


if __name__ == "__main__":
    asyncio.run(main())
