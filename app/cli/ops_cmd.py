"""
`proteus jobs` and `proteus memory` — the two stateful subsystems.

Both were previously reachable only by the agent itself, through tools. That is
fine until something goes wrong, at which point "what has this thing scheduled,
and what does it think it knows about me" needs an answer that is not psql.

`memory forget` also matters for a reason beyond debugging: when a person asks
you to delete what you hold about them, you need a command, not a query you
compose under pressure.
"""
from __future__ import annotations

import asyncio
from typing import Any

import typer

from ._common import confirm_tty, die, emit, err, load_env, out, table

jobs_app = typer.Typer(no_args_is_help=True, help="Inspect and cancel scheduled jobs.")
memory_app = typer.Typer(no_args_is_help=True, help="Inspect and clear per-user memory.")


async def _with_db(coro_fn):
    """Open the pool, run, always close. The CLI is short-lived; leaks show up
    as a connection slot the gateway then cannot have."""
    from app import db

    if not await db.try_init_pool(quiet=True):
        die("no database connection", "Scheduled jobs and memory both need DATABASE_URL.")
    try:
        return await coro_fn()
    finally:
        await db.close_pool()


# ── jobs ─────────────────────────────────────────────────────────────────────

@jobs_app.command("list")
def jobs_list(user: str = typer.Option(None, "--user", "-u", help="Only this user_key."),
              as_json: bool = typer.Option(False, "--json")) -> None:
    """Show scheduled jobs, across all users unless --user is given."""
    load_env()

    async def go():
        from app import db

        sql = ("SELECT id, user_key, channel, target, prompt, cron, next_run, enabled, last_run "
               "FROM proteus.proteus_cron")
        args: list[Any] = []
        if user:
            sql += " WHERE user_key = $1"
            args.append(user)
        sql += " ORDER BY next_run"
        async with db.get_pool().acquire() as c:
            return [dict(r) for r in await c.fetch(sql, *args)]

    rows = asyncio.run(_with_db(go))

    def render(rows):
        if not rows:
            out.print("[dim]no scheduled jobs[/]")
            return
        t = table("id", "user", "when", "next run", "target", "prompt")
        for r in rows:
            when = r["cron"] or "one-off"
            t.add_row(str(r["id"]), r["user_key"][:22], when,
                      str(r["next_run"])[:19],
                      f"{r['channel']}:{str(r['target'])[:24]}",
                      str(r["prompt"])[:40])
        out.print(t)
        out.print(f"\n[dim]{len(rows)} job(s)[/]")

    emit(rows, as_json, render)


@jobs_app.command("cancel")
def jobs_cancel(job_id: int = typer.Argument(..., help="Job id from `proteus jobs list`."),
                yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Cancel a scheduled job."""
    load_env()

    async def go():
        from app import cron, db

        async with db.get_pool().acquire() as c:
            row = await c.fetchrow(
                "SELECT id, user_key, prompt FROM proteus.proteus_cron WHERE id=$1", job_id)
            if row is None:
                return None
            if not yes and not confirm_tty(f"Cancel job {job_id} ({str(row['prompt'])[:50]}…)?"):
                raise typer.Exit(1)
            await c.execute("DELETE FROM proteus.proteus_cron WHERE id=$1", job_id)
        await cron._bump_version()      # the scheduler caches "next due"; invalidate it
        return dict(row)

    row = asyncio.run(_with_db(go))
    if row is None:
        die(f"no job with id {job_id}", "List them: proteus jobs list")
    out.print(f"[green]cancelled[/] job {job_id} ({row['user_key']})")


@jobs_app.command("run")
def jobs_run(job_id: int = typer.Argument(..., help="Job id to run immediately.")) -> None:
    """Run a job now, without waiting for its schedule.

    Useful for checking a job actually does what you meant before leaving it to
    fire at 06:00. It runs with the owner's privileges, exactly as the scheduler
    would, and delivers to the job's real target.
    """
    load_env()

    async def go():
        from app import cron, db

        async with db.get_pool().acquire() as c:
            row = await c.fetchrow(
                "SELECT id, user_key, channel, target, prompt, cron FROM proteus.proteus_cron "
                "WHERE id=$1", job_id)
        if row is None:
            return None
        err.print(f"[dim]running job {job_id}: {str(row['prompt'])[:60]}…[/]")
        await cron.run_job(dict(row))
        return dict(row)

    row = asyncio.run(_with_db(go))
    if row is None:
        die(f"no job with id {job_id}", "List them: proteus jobs list")
    out.print(f"[green]ran[/] job {job_id}; delivered to {row['channel']}:{row['target']}")


# ── memory ───────────────────────────────────────────────────────────────────

@memory_app.command("show")
def memory_show(user: str = typer.Argument(..., help="user_key, e.g. 'alice' or 'telegram:123'."),
                limit: int = typer.Option(50, "--limit", "-n"),
                as_json: bool = typer.Option(False, "--json")) -> None:
    """Show what the gateway has stored about one user."""
    load_env()

    async def go():
        from app.memory import store

        return {
            "user": user,
            "long_term": await store.list_memories(user, limit),
            "recent_messages": await store.recent_messages(user, limit),
            "turns": await store.user_turn_count(user),
        }

    data = asyncio.run(_with_db(go))

    def render(d):
        out.print(f"[bold]{d['user']}[/]  [dim]{d['turns']} turns recorded[/]")
        out.print(f"\n[bold]long-term memory[/] ({len(d['long_term'])})")
        for m in d["long_term"]:
            out.print(f"  [dim]{m.get('id')}[/] {str(m.get('text'))[:90]}")
        if not d["long_term"]:
            out.print("  [dim]nothing distilled yet[/]")
        out.print(f"\n[bold]working memory[/] ({len(d['recent_messages'])} most recent)")
        for m in d["recent_messages"][-10:]:
            out.print(f"  [dim]{m.get('role', '?'):9}[/] {str(m.get('content'))[:80]}")

    emit(data, as_json, render)


@memory_app.command("search")
def memory_search(user: str = typer.Argument(...),
                  query: str = typer.Argument(..., help="What to look for."),
                  limit: int = typer.Option(5, "--limit", "-n"),
                  as_json: bool = typer.Option(False, "--json")) -> None:
    """Semantic search over a user's long-term memory, as the agent would do it."""
    load_env()

    async def go():
        from app import config
        from app.memory import embed, store

        vector = await embed.embed(query)
        hits = await store.recall(user, vector, limit, config.MEMORY_RECALL_MIN_SCORE)
        return {"user": user, "query": query,
                "hits": [{"text": h["text"], "score": round(float(h["score"]), 3)} for h in hits]}

    data = asyncio.run(_with_db(go))

    def render(d):
        if not d["hits"]:
            out.print(f"[dim]nothing above the recall threshold for {d['query']!r}[/]")
            return
        for h in d["hits"]:
            out.print(f"  [dim]{h['score']:.3f}[/]  {h['text'][:100]}")

    emit(data, as_json, render)


@memory_app.command("forget")
def memory_forget(user: str = typer.Argument(...),
                  everything: bool = typer.Option(False, "--all",
                                                  help="Long-term memory too, not just the conversation."),
                  yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete a user's memory. --all includes distilled long-term facts."""
    load_env()
    what = "ALL memory (conversation and long-term facts)" if everything else "the conversation history"
    if not yes and not confirm_tty(f"Delete {what} for {user!r}?"):
        raise typer.Exit(1)

    async def go():
        from app import db
        from app.memory import store

        messages = await store.clear_messages(user)
        facts = 0
        if everything:
            async with db.get_pool().acquire() as c:
                result = await c.execute(
                    "DELETE FROM proteus.proteus_memory WHERE user_key=$1", user)
                facts = int(result.split()[-1] or 0)
        return {"messages": messages, "facts": facts}

    d = asyncio.run(_with_db(go))
    out.print(f"[green]deleted[/] {d['messages']} messages"
              + (f" and {d['facts']} long-term memories" if everything else ""))


@memory_app.command("users")
def memory_users(as_json: bool = typer.Option(False, "--json")) -> None:
    """List every user the gateway holds memory for."""
    load_env()

    async def go():
        from app import db

        async with db.get_pool().acquire() as c:
            return [dict(r) for r in await c.fetch(
                """SELECT user_key,
                          count(*) FILTER (WHERE src='m') AS messages,
                          count(*) FILTER (WHERE src='f') AS facts,
                          max(created_at) AS last_seen
                   FROM (
                     SELECT user_key, created_at, 'm' AS src FROM proteus.proteus_message
                     UNION ALL
                     SELECT user_key, created_at, 'f' AS src FROM proteus.proteus_memory
                   ) t
                   GROUP BY user_key ORDER BY max(created_at) DESC""")]

    rows = asyncio.run(_with_db(go))

    def render(rows):
        if not rows:
            out.print("[dim]no stored memory[/]")
            return
        t = table("user", "messages", "facts", "last seen")
        for r in rows:
            t.add_row(str(r["user_key"])[:34], str(r["messages"]),
                      str(r["facts"]), str(r["last_seen"])[:19])
        out.print(t)

    emit(rows, as_json, render)
