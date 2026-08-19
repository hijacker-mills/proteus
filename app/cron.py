"""
Cron / scheduled tasks.

Jobs are persisted in Postgres (`proteus.proteus_cron`). Each job runs a natural-language
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

from . import agent, config, memory, redisc
from .db import available as db_available, get_pool

logger = logging.getLogger("proteus.cron")

_DDL = [
    # Own schema, not `public` — Prisma owns public and drops anything it doesn't
    # model there. See app/memory/store.py for the full story.
    "CREATE SCHEMA IF NOT EXISTS proteus",
    """
    CREATE TABLE IF NOT EXISTS proteus.proteus_cron (
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
    "CREATE INDEX IF NOT EXISTS proteus_cron_due ON proteus.proteus_cron (enabled, next_run)",
    "CREATE INDEX IF NOT EXISTS proteus_cron_user ON proteus.proteus_cron (user_key)",
]

_scheduler: asyncio.Task | None = None

# ── the "don't poll Postgres" gate ───────────────────────────────────────────
#
# The scheduler ticks every CRON_CHECK_INTERVAL (30s). Sending that tick to
# Postgres kept a serverless (Neon) compute awake 24/7: it bills a minimum
# 5-minute idle tail after every query, so ANY timer under 5 minutes pins the
# compute at 100% duty cycle. 2,880 SELECTs a day that almost always return
# zero rows burned the entire monthly compute allowance in ~16 days.
#
# So the tick now gates on a cached "when is the next job due" answer and only
# reaches for Postgres when a job is actually due. The cache lives in this
# process (the scheduler is a singleton — see the module docstring); Redis
# carries only an invalidation counter, so a job created by one of the uvicorn
# workers is still picked up promptly. If Redis is unreachable the local cache
# still holds, and _GATE_MAX_AGE bounds how stale it can get.
_CRON_VERSION_KEY = "proteus:cron:version"
_GATE_MAX_AGE = timedelta(hours=6)   # self-heal even if an invalidation is lost

_gate_next_due: datetime | None = None   # earliest enabled next_run; None = no jobs
_gate_version: str | None = None         # Redis counter this cache was built against
_gate_at: datetime | None = None         # when the cache was last refreshed


async def _cron_version() -> str | None:
    """Read the shared invalidation counter. None when Redis is unavailable."""
    try:
        r = await redisc.get_redis()
        if r is None:
            return None
        return await r.get(_CRON_VERSION_KEY)
    except Exception:
        return None


async def _bump_version() -> None:
    """Invalidate every scheduler's cache after a job is created or deleted."""
    global _gate_at
    _gate_at = None                      # this process re-reads on its next tick
    try:
        r = await redisc.get_redis()
        if r is not None:
            await r.incr(_CRON_VERSION_KEY)
    except Exception:
        logger.debug("could not bump cron version in redis", exc_info=True)


async def _needs_db(now: datetime) -> bool:
    """Whether this tick has to touch Postgres. Cheap: at most one Redis GET."""
    global _gate_version
    if _gate_at is None:
        return True                                   # nothing cached yet
    if now - _gate_at > _GATE_MAX_AGE:
        return True                                   # cache too old to trust
    if _gate_next_due is not None and now >= _gate_next_due:
        return True                                   # a job is due
    version = await _cron_version()
    if version != _gate_version:
        _gate_version = version
        return True                                   # someone changed the jobs
    return False


async def _refresh_gate(conn, now: datetime) -> None:
    """Cache the earliest upcoming next_run so later ticks can skip Postgres."""
    global _gate_next_due, _gate_version, _gate_at
    _gate_next_due = await conn.fetchval(
        "SELECT min(next_run) FROM proteus.proteus_cron WHERE enabled"
    )
    _gate_version = await _cron_version()
    _gate_at = now


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

_NO_DB = "scheduling is unavailable — this deployment has no database connection right now"


async def create_job(user_key: str, channel: str, target: str, prompt: str,
                     cron: str | None = None, in_seconds: int | None = None) -> dict:
    if not db_available():
        return {"error": _NO_DB}
    # Each job is a model call on a timer, so an unbounded list is a way to spend
    # someone else's provider quota. Cheap count, checked before anything else.
    async with get_pool().acquire() as conn:
        existing = await conn.fetchval(
            "SELECT count(*) FROM proteus.proteus_cron WHERE user_key=$1 AND enabled", user_key)
    if existing >= config.MAX_JOBS_PER_USER:
        return {"error": f"you already have {existing} scheduled jobs "
                         f"(limit {config.MAX_JOBS_PER_USER}); cancel one first"}
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
            """INSERT INTO proteus.proteus_cron (user_key, channel, target, prompt, cron, next_run)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
            user_key, channel, target, prompt, cron, next_run,
        )
    await _bump_version()   # the scheduler's cached "next due" is now stale
    return {"ok": True, "id": row["id"], "next_run": next_run.isoformat(),
            "recurring": bool(cron)}


async def list_jobs(user_key: str) -> list[dict]:
    if not db_available():
        return []
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, prompt, cron, next_run, enabled FROM proteus.proteus_cron WHERE user_key=$1 ORDER BY next_run",
            user_key,
        )
    return [{"id": r["id"], "prompt": r["prompt"], "cron": r["cron"],
             "next_run": r["next_run"].isoformat(), "enabled": r["enabled"]} for r in rows]


async def delete_job(job_id: int, user_key: str) -> dict:
    if not db_available():
        return {"error": _NO_DB}
    async with get_pool().acquire() as conn:
        res = await conn.execute("DELETE FROM proteus.proteus_cron WHERE id=$1 AND user_key=$2", job_id, user_key)
    await _bump_version()   # the scheduler's cached "next due" is now stale
    return {"ok": res.split()[-1] != "0", "id": job_id}


# ── scheduler ────────────────────────────────────────────────────────────────

async def _advance(job: dict, now: datetime) -> None:
    async with get_pool().acquire() as conn:
        if job["cron"]:
            await conn.execute("UPDATE proteus.proteus_cron SET next_run=$1, last_run=$2 WHERE id=$3",
                               _cron_next(job["cron"], now), now, job["id"])
        else:
            await conn.execute("DELETE FROM proteus.proteus_cron WHERE id=$1", job["id"])  # one-off done


async def run_job(job: dict) -> None:
    from . import channels  # lazy to avoid import cycle

    # A job runs with the privileges of its owner, re-checked NOW — not with the
    # scheduler's. Otherwise anyone who could create a job (the `schedule` tool)
    # would have a delayed path to host tools they can't call directly.
    _, _, sender = job["user_key"].partition(":")
    host_tools = channels.is_trusted(job["channel"], sender)

    messages, extra = await memory.prepare(job["user_key"], job["prompt"])
    parts: list[str] = []
    try:
        async for ev in agent.run(job["user_key"], messages, extra_system=extra, host_tools=host_tools):
            if ev["type"] == "text":
                parts.append(ev["text"])
            elif ev["type"] == "error":
                parts.append(f"⚠️ {ev['message']}")
    except Exception as exc:
        logger.exception("cron job %s failed", job["id"])
        parts.append(f"⚠️ {exc}")
    reply = "".join(parts).strip() or "(scheduled task produced no output)"

    if job["channel"] == "webhook":
        # HTTP callers have no push channel, so the result is POSTed back to a
        # URL they nominated. It goes through the SSRF guard at FIRE time, not
        # just at creation: DNS can be repointed at an internal address in the
        # interval between scheduling a job and it running.
        from .httpclient import get_client
        from .tools.url_safety import is_safe_url_async

        if not await is_safe_url_async(job["target"]):
            logger.warning("cron job %s: refusing webhook to %s", job["id"], job["target"])
            return
        try:
            await get_client().post(job["target"], json={
                "job_id": job["id"], "user": job["user_key"],
                "prompt": job["prompt"], "reply": reply})
            logger.info("cron job %s delivered to webhook (%d chars)", job["id"], len(reply))
        except Exception as exc:
            logger.warning("cron job %s webhook failed: %s", job["id"], exc)
        return

    await channels.deliver(job["channel"], job["target"], "⏰ " + reply)
    logger.info("cron job %s delivered to %s:%s (%d chars)", job["id"], job["channel"], job["target"], len(reply))


async def _tick() -> None:
    if not db_available():
        return  # database is optional; jobs resume when it reconnects
    now = _now()
    if not await _needs_db(now):
        return  # nothing is due — don't wake the database just to be told so
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, user_key, channel, target, prompt, cron FROM proteus.proteus_cron WHERE enabled AND next_run <= $1",
            now,
        )
    for r in rows:
        job = dict(r)
        await _advance(job, now)               # advance/delete first → no double-fire
        asyncio.create_task(_safe_run(job))
    async with get_pool().acquire() as conn:
        await _refresh_gate(conn, now)         # AFTER _advance, so next_run is current


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
