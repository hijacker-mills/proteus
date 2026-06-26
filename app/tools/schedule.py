"""
schedule tool — lets the agent create/list/cancel scheduled tasks (cron).

The caller's user_id is the channel key ("telegram:8707843599"), from which we
derive where to deliver. Scheduling is only meaningful inside a channel.
"""
from __future__ import annotations

from typing import Any

from .. import cron


async def schedule(user_id: str, action: str, prompt: str | None = None,
                   cron_expr: str | None = None, in_seconds: int | None = None,
                   job_id: int | None = None) -> dict[str, Any]:
    channel, _, target = user_id.partition(":")
    if action == "create":
        if not prompt:
            return {"error": "prompt required"}
        if channel not in ("telegram", "whatsapp", "signal") or not target:
            return {"error": "scheduling is only available inside a chat channel"}
        return await cron.create_job(user_id, channel, target, prompt,
                                     cron=cron_expr, in_seconds=in_seconds)
    if action == "list":
        return {"jobs": await cron.list_jobs(user_id)}
    if action in ("cancel", "delete"):
        if job_id is None:
            return {"error": "id required to cancel"}
        return await cron.delete_job(int(job_id), user_id)
    return {"error": f"unknown schedule action: {action}"}
