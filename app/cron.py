"""
Cron / scheduled tasks.

Jobs are persisted in Postgres (`acag_cron`). Each job runs a natural-language
prompt through the agent (full tools + memory) at its scheduled time and delivers
the result to the channel it was created from. The scheduler loop runs in the
SINGLE channels_runner process (so jobs fire once), while any process can create
jobs via the `schedule` tool.

Schedules: a cron expression (recurring, interpreted in CRON_TZ) or a one-off
delay (`in_seconds`). One-offs are deleted after firing; recurring jobs advance
to their next occurrence.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from croniter import croniter

from . import agent, config, memory
from .db import get_pool

logger = logging.getLogger("acag.cron")

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS acag_cron (
        id         BIGSERIAL PRIMARY KEY,
        user_key   TEXT        NOT NULL,
        channel    TEXT        NOT NULL,
        target     TEXT        NOT NULL,
        prompt     TEXT        NOT NULL,
        cron       TEXT,
        next_run   TIMESTAMPTZ NOT NULL,
        enabled    BOOLEAN     NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_run   TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS acag_cron_due ON acag_cron (enabled, next_run)",
    "CREATE INDEX IF NOT EXISTS acag_cron_user ON acag_cron (user_key)",
]

_scheduler: asyncio.Task | None = None


async def ensure_schema() -> None:
    async with get_pool().acquire() as conn:
        for stmt in _DDL:
            await conn.execute(stmt)


def _tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(config.CRON_TZ)
    except Exception:
        return timezone.utc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cron_next(expr: str, base: datetime | None = None) -> datetime:
    base = (base or _now()).astimezone(_tz())
    return croniter(expr, base).get_next(datetime).astimezone(timezone.utc)


# ── CRUD ─────────────────────────────────────────────────────────────────────

async def create_job(user_key: str, channel: str, target: str, prompt: str,
                     cron: str | None = None, in_seconds: int | None = None) -> dict:
    if cron:
        if not croniter.is_valid(cron):
            return {"error": f"invalid cron expression: {cron}"}
        next_run = _cron_next(cron)
    elif in_seconds:
        next_run = _now() + timedelta(seconds=max(5, int(in_seconds)))
    else:
        return {"error": "provide either 'cron' (recurring) or 'in_seconds' (one-off)"}

    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO acag_cron (user_key, channel, target, prompt, cron, next_run)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
            user_key, channel, target, prompt, cron, next_run,
        )
    return {"ok": True, "id": row["id"], "next_run": next_run.isoformat(),
            "recurring": bool(cron)}


async def list_jobs(user_key: str) -> list[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, prompt, cron, next_run, enabled FROM acag_cron WHERE user_key=$1 ORDER BY next_run",
            user_key,
        )
    return [{"id": r["id"], "prompt": r["prompt"], "cron": r["cron"],
             "next_run": r["next_run"].isoformat(), "enabled": r["enabled"]} for r in rows]


async def delete_job(job_id: int, user_key: str) -> dict:
    async with get_pool().acquire() as conn:
        res = await conn.execute("DELETE FROM acag_cron WHERE id=$1 AND user_key=$2", job_id, user_key)
    return {"ok": res.split()[-1] != "0", "id": job_id}


# ── scheduler ────────────────────────────────────────────────────────────────

async def _advance(job: dict, now: datetime) -> None:
    async with get_pool().acquire() as conn:
        if job["cron"]:
            await conn.execute("UPDATE acag_cron SET next_run=$1, last_run=$2 WHERE id=$3",
                               _cron_next(job["cron"], now), now, job["id"])
        else:
            await conn.execute("DELETE FROM acag_cron WHERE id=$1", job["id"])  # one-off done


async def run_job(job: dict) -> None:
    from . import channels  # lazy to avoid import cycle
    messages, extra = await memory.prepare(job["user_key"], job["prompt"])
    parts: list[str] = []
    try:
        async for ev in agent.run(job["user_key"], messages, extra_system=extra):
            if ev["type"] == "text":
                parts.append(ev["text"])
            elif ev["type"] == "error":
                parts.append(f"⚠️ {ev['message']}")
    except Exception as exc:
        logger.exception("cron job %s failed", job["id"])
        parts.append(f"⚠️ {exc}")
    reply = "".join(parts).strip() or "(scheduled task produced no output)"
    await channels.deliver(job["channel"], job["target"], "⏰ " + reply)
    logger.info("cron job %s delivered to %s:%s (%d chars)", job["id"], job["channel"], job["target"], len(reply))


async def _tick() -> None:
    now = _now()
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, user_key, channel, target, prompt, cron FROM acag_cron WHERE enabled AND next_run <= $1",
            now,
        )
    for r in rows:
        job = dict(r)
        await _advance(job, now)               # advance/delete first → no double-fire
        asyncio.create_task(_safe_run(job))


async def _safe_run(job: dict) -> None:
    try:
        await run_job(job)
    except Exception:
        logger.exception("cron run failed for job %s", job.get("id"))


async def _loop() -> None:
    logger.info("cron scheduler started (tz=%s, every %ds)", config.CRON_TZ, config.CRON_CHECK_INTERVAL)
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            logger.info("cron scheduler stopped")
            raise
        except Exception:
            logger.exception("cron tick error")
        await asyncio.sleep(config.CRON_CHECK_INTERVAL)


async def start_scheduler() -> None:
    global _scheduler
    if config.CRON_ENABLED and _scheduler is None:
        _scheduler = asyncio.create_task(_loop())


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.cancel()
        try:
            await _scheduler
        except (asyncio.CancelledError, Exception):
            pass
        _scheduler = None
