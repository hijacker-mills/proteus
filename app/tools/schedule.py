"""
schedule tool — the agent creating, listing and cancelling its own future work.

WHERE THE RESULT GOES. A scheduled run produces an answer with nobody waiting
for it, so every job needs somewhere to deliver:

  from a channel   back to that chat, since the sender is reachable there.
  over HTTP        POSTed to a `webhook_url` the caller supplies. HTTP has no
                   push channel, so without this an HTTP-first deployment can
                   create jobs whose output has nowhere to go.

The webhook is SSRF-checked when the job FIRES, not only when it is created.
"""
from __future__ import annotations

from typing import Any

from .. import cron


async def schedule(user_id: str, action: str, prompt: str | None = None,
                   cron_expr: str | None = None, in_seconds: int | None = None,
                   job_id: int | None = None, webhook_url: str | None = None) -> dict[str, Any]:
    channel, _, target = user_id.partition(":")
    if action == "create":
        if not prompt:
            return {"error": "prompt required"}
        if channel in ("telegram", "whatsapp", "signal") and target:
            return await cron.create_job(user_id, channel, target, prompt,
                                         cron=cron_expr, in_seconds=in_seconds)
        if webhook_url:
            from .url_safety import is_safe_url_async
            if not await is_safe_url_async(webhook_url):
                return {"error": "that webhook url is not allowed (private or metadata address)"}
            return await cron.create_job(user_id, "webhook", webhook_url, prompt,
                                         cron=cron_expr, in_seconds=in_seconds)
        return {"error": "provide webhook_url — a scheduled run has no one waiting for it, "
                         "so it needs somewhere to deliver the result"}
    if action == "list":
        return {"jobs": await cron.list_jobs(user_id)}
    if action in ("cancel", "delete"):
        if job_id is None:
            return {"error": "id required to cancel"}
        return await cron.delete_job(int(job_id), user_id)
    return {"error": f"unknown schedule action: {action}"}
