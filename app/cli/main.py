"""proteus CLI — many agents, one gateway."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import typer

from . import agents as agents_cmd
from . import auth_cmd, ops_cmd, tools_cmd
from ..identity import signed_headers
from ._common import (REPO, api_key, base_url, die, emit, err, get_json,
                      load_env, out, table, try_json, version)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=True,          # `proteus --install-completion` comes free
    help="Many agents, one gateway. Manage proteus agents, tools and a running instance.",
)
app.add_typer(agents_cmd.app, name="agent")
app.add_typer(tools_cmd.app, name="tool")
app.add_typer(auth_cmd.app, name="auth")
app.add_typer(ops_cmd.jobs_app, name="jobs")
app.add_typer(ops_cmd.memory_app, name="memory")


def _version_cb(value: bool) -> None:
    if value:
        out.print(f"proteus {version()}", highlight=False)
        raise typer.Exit()


@app.callback()
def _root(
    _v: bool = typer.Option(False, "--version", "-V", callback=_version_cb, is_eager=True,
                            help="Show the version and exit."),
) -> None:
    pass


# ── the running gateway ──────────────────────────────────────────────────────

@app.command()
def health(remote: str = typer.Option(None, "--remote", help="Gateway URL."),
           as_json: bool = typer.Option(False, "--json")) -> None:
    """Show /healthz, formatted. Exits non-zero when the gateway is down."""
    base = base_url(remote)
    h = get_json(f"{base}/healthz")

    def render(h):
        colour = {"ok": "green", "degraded": "yellow", "down": "red"}.get(h.get("status"), "white")
        out.print(f"[{colour}]{str(h.get('status', '?')).upper()}[/]  {base}")
        out.print(f"  model      : {h.get('model')}")
        out.print(f"  database   : {'connected' if h.get('db') else 'not connected (chat still works)'}")
        out.print(f"  toolset    : {h.get('toolset')}")
        c = h.get("concurrency") or {}
        out.print(f"  in flight  : {c.get('in_use')}/{c.get('limit')} per worker  "
                  f"(rejected: {c.get('rejected')})")
        out.print(f"  channels   : {', '.join(h.get('channels') or []) or 'none'}")
        auth = h.get("model_auth") or {}
        if auth.get("detail"):
            out.print(f"  [yellow]{auth['detail']}[/]")

    emit(h, as_json, render)
    if h.get("status") == "down":
        raise typer.Exit(1)


@app.command()
def serve(host: str = typer.Option(None, "--host"),
          port: int = typer.Option(None, "--port", "-p"),
          workers: int = typer.Option(None, "--workers", "-w"),
          reload: bool = typer.Option(False, "--reload", help="Auto-reload on code change (dev).")) -> None:
    """Run the gateway."""
    load_env()
    from app import config

    if not config.API_KEY:
        err.print("[yellow]warning:[/] API_KEY is empty — this gateway is unauthenticated.")
    if config.MODEL.startswith("mock/"):
        err.print(f"[yellow]warning:[/] MODEL is {config.MODEL}, a synthetic backend.")

    cmd = [sys.executable, "-m", "uvicorn", "app.main:app",
           "--host", host or config.HOST,
           "--port", str(port or config.PORT),
           "--no-access-log", "--timeout-keep-alive", "75"]
    if reload:
        cmd += ["--reload"]           # uvicorn refuses --reload alongside --workers
    else:
        cmd += ["--workers", str(workers or config.WORKERS),
                "--loop", "uvloop", "--http", "httptools"]

    os.chdir(REPO)
    try:
        raise typer.Exit(subprocess.call(cmd))
    except KeyboardInterrupt:
        raise typer.Exit(0)


@app.command()
def chat(agent: str = typer.Argument(None, help="Agent to talk to; omit for the default."),
         remote: str = typer.Option(None, "--remote"),
         user: str = typer.Option("cli-user", "--user", help="user_id every tool call is scoped to."),
         mode: str = typer.Option(None, "--mode", help="One of the agent's named modes.")) -> None:
    """Interactive chat against a running gateway. /exit or Ctrl-D to leave."""
    import httpx

    base = base_url(remote)
    if agent:
        profile_header = agent
    else:
        profile_header = None
    if mode:
        mode_header = mode
    else:
        mode_header = None

    history: list[dict] = []
    out.print(f"[dim]{base}  agent={agent or 'default'}  user={user}"
              f"{'  mode=' + mode if mode else ''}[/]")
    out.print("[dim]/reset clears history · /history shows it · /exit quits[/]\n")

    with httpx.Client(timeout=300) as client:
        while True:
            try:
                msg = input("you › ").strip()
            except (EOFError, KeyboardInterrupt):
                out.print("\n[dim]bye[/]")
                return
            if not msg:
                continue
            if msg in ("/exit", "/quit"):
                return
            if msg == "/reset":
                history.clear()
                out.print("[dim]history cleared[/]\n")
                continue
            if msg == "/history":
                out.print(f"[dim]{len(history)} messages[/]")
                for m in history:
                    out.print(f"[dim]  {m['role']:9}[/] {m['content'][:100]}")
                out.print()
                continue

            history.append({"role": "user", "content": msg})
            reply, t0, first = [], time.time(), None
            try:
                hdr = {"Authorization": f"Bearer {api_key()}", **signed_headers(user)}
                if profile_header:
                    hdr["X-Proteus-Profile"] = profile_header
                if mode_header:
                    hdr["X-Proteus-Mode"] = mode_header
                with client.stream("POST", f"{base}/v1/chat/completions", headers=hdr,
                                   json={"stream": True, "messages": history}) as r:
                    if r.status_code != 200:
                        body = r.read().decode("utf-8", "replace")[:300]
                        err.print(f"[red]HTTP {r.status_code}[/] {body}")
                        history.pop()
                        continue
                    sys.stdout.write("\nproteus › ")
                    sys.stdout.flush()
                    for line in r.iter_lines():
                        if not line.startswith("data:") or line == "data: [DONE]":
                            continue
                        d = json.loads(line[5:])
                        if "proteus_tool_event" in d:
                            e = d["proteus_tool_event"]
                            colour = "green" if e.get("status") == "ok" else "red"
                            out.print(f"\n[dim]  ⚙ [/][{colour}]{e['tool']}[/]"
                                      f"[dim] {e.get('status')} {e.get('ms')}ms[/]")
                            continue
                        chunk = d["choices"][0]["delta"].get("content")
                        if chunk:
                            if first is None:
                                first = time.time() - t0
                            sys.stdout.write(chunk)
                            sys.stdout.flush()
                            reply.append(chunk)
                print()
            except KeyboardInterrupt:
                out.print("\n[dim]interrupted[/]")
            except httpx.HTTPError as exc:
                err.print(f"[red]connection failed:[/] {exc}")
                history.pop()
                continue

            text = "".join(reply)
            out.print(f"[dim]  {len(text)} chars"
                      f"{f' · first token {first * 1000:.0f}ms' if first else ''}"
                      f" · {time.time() - t0:.1f}s[/]\n")
            history.append({"role": "assistant", "content": text})


@app.command()
def bench(concurrency: int = typer.Option(20, "--concurrency", "-c"),
          n: int = typer.Option(100, "-n", help="Total requests."),
          stream: bool = typer.Option(False, "--stream", help="Use SSE instead of JSON."),
          remote: str = typer.Option(None, "--remote"),
          as_json: bool = typer.Option(False, "--json")) -> None:
    """Load-test a running gateway.

    Self-contained, so it works from an installed package rather than needing
    the test directory. Point MODEL at a `mock/*` backend first unless you mean
    to spend real tokens; the numbers then measure the gateway, not the provider.
    """
    import httpx

    base, key = base_url(remote), api_key()
    h = get_json(f"{base}/healthz")
    if not str(h.get("model", "")).startswith("mock/"):
        err.print(f"[yellow]warning:[/] MODEL is {h.get('model')} — this spends real tokens.")

    async def one(client, i: int) -> tuple[bool, float]:
        t0 = time.time()
        hdr = {"Authorization": f"Bearer {key}", **signed_headers(f"bench-{i % 50}")}
        body: dict = {"messages": [{"role": "user", "content": "hello"}]}
        try:
            if stream:
                body["stream"] = True
                async with client.stream("POST", f"{base}/v1/chat/completions",
                                         headers=hdr, json=body) as r:
                    ok = r.status_code == 200
                    async for _ in r.aiter_lines():
                        pass
            else:
                r = await client.post(f"{base}/v1/chat/completions", headers=hdr, json=body)
                ok = r.status_code == 200 and bool(r.json()["choices"][0]["message"]["content"])
            return ok, time.time() - t0
        except Exception:
            return False, time.time() - t0

    async def run_bench() -> dict:
        sem = asyncio.Semaphore(concurrency)
        results: list[tuple[bool, float]] = []
        limits = httpx.Limits(max_connections=concurrency + 20)
        async with httpx.AsyncClient(timeout=300, limits=limits) as client:
            async def guarded(i: int) -> None:
                async with sem:
                    results.append(await one(client, i))
            t0 = time.time()
            await asyncio.gather(*[guarded(i) for i in range(n)])
            wall = time.time() - t0

        good = sorted(d for ok, d in results if ok)
        def pct(q: float) -> float:
            return good[min(int(len(good) * q), len(good) - 1)] if good else 0.0
        return {"requests": n, "concurrency": concurrency, "streaming": stream,
                "wall_seconds": round(wall, 2),
                "throughput_rps": round(n / wall, 1) if wall else 0,
                "succeeded": len(good), "failed": n - len(good),
                "p50_ms": round(pct(0.50) * 1000), "p95_ms": round(pct(0.95) * 1000),
                "p99_ms": round(pct(0.99) * 1000)}

    if not as_json:
        out.print(f"[dim]{n} requests, {concurrency} concurrent, "
                  f"{'SSE' if stream else 'JSON'} → {base}[/]")
    stats = asyncio.run(run_bench())

    def render(s):
        t = table("metric", "value")
        t.add_row("wall", f"{s['wall_seconds']}s")
        t.add_row("throughput", f"{s['throughput_rps']} req/s")
        t.add_row("succeeded", f"{s['succeeded']}/{s['requests']}")
        t.add_row("failed", str(s["failed"]))
        t.add_row("p50 / p95 / p99", f"{s['p50_ms']} / {s['p95_ms']} / {s['p99_ms']} ms")
        out.print(t)

    emit(stats, as_json, render)
    if stats["failed"]:
        raise typer.Exit(1)


@app.command()
def login() -> None:
    """ChatGPT (Codex) OAuth device login. Alias of `proteus auth login`."""
    auth_cmd.login()


@app.command()
def config(as_json: bool = typer.Option(False, "--json"),
           show_secrets: bool = typer.Option(False, "--show-secrets",
                                             help="Print secrets in full. Careful.")) -> None:
    """Show the configuration actually in effect, with secrets masked.

    Not the same as reading .env: this is what the process resolved after
    defaults and environment overrides, which is usually where the surprise is.
    """
    load_env()
    from app import config as cfg

    SECRET = ("KEY", "TOKEN", "SECRET", "PASSWORD", "DSN", "DATABASE_URL")

    def show(name: str, value):
        if not isinstance(value, str) or not value:
            return value
        if show_secrets or not any(s in name for s in SECRET):
            return value
        if name == "DATABASE_URL":
            # Keep the shape (host/db) and lose the password — that is the bit
            # you need when something cannot connect.
            import re
            return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", value)
        return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "***"

    data = {k: show(k, getattr(cfg, k)) for k in sorted(dir(cfg))
            if k.isupper() and not k.startswith("_")}

    def render(d):
        groups = {
            "model": ("MODEL", "MAX_TOKENS", "TEMPERATURE", "MAX_TOOL_TURNS",
                      "REQUEST_TIMEOUT", "LLM_RETRIES", "PROMPT_CACHING"),
            "server": ("HOST", "PORT", "WORKERS", "API_KEY", "ADMIN_API_KEY"),
            "limits": ("MAX_CONCURRENT_COMPLETIONS", "CONCURRENCY_WAIT",
                       "MAX_PARALLEL_TOOLS", "STREAM_COALESCE_MS", "MAX_JOBS_PER_USER"),
            "agents": ("DEFAULT_PROFILE", "AGENTS_DIR", "TOOLS_DIR", "TOOLSET",
                       "SYSTEM_PROMPT_FILE"),
            "storage": ("DATABASE_URL", "DB_POOL_MIN", "DB_POOL_MAX", "REDIS_URL"),
            "tools": ("TOOLS_BROWSER", "TOOLS_SHELL", "TOOLS_CODE_EXEC", "TOOLS_EMAIL",
                      "FILES_ROOT", "ALLOW_PRIVATE_URLS", "SEARCH_URL_TEMPLATE"),
            "cron": ("CRON_ENABLED", "CRON_IN_WEB", "CRON_TZ", "CRON_CHECK_INTERVAL"),
        }
        shown = set()
        for group, keys in groups.items():
            rows = [(k, d[k]) for k in keys if k in d]
            if not rows:
                continue
            out.print(f"\n[bold]{group}[/]")
            for k, v in rows:
                shown.add(k)
                out.print(f"  {k:28} {v}")
        rest = sorted(set(d) - shown)
        if rest:
            out.print("\n[dim]other[/]")
            for k in rest:
                out.print(f"  [dim]{k:28} {d[k]}[/]")

    emit(data, as_json, render)


@app.command()
def tui(remote: str = typer.Option(None, "--remote"),
        user: str = typer.Option("tui-user", "--user")) -> None:
    """Full-screen terminal UI for trying agents out."""
    try:
        from .tui import run_tui
    except ImportError as exc:
        die(f"the TUI needs textual ({exc})", "Install it:  pip install 'proteus-gateway[tui]'")
    run_tui(base_url(remote), api_key(), user)


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json")) -> None:
    """Check configuration and connectivity. Exits non-zero if anything is wrong."""
    load_env()
    from app import agents_store, config, toolsets
    from app.tools import declarative

    problems: list[str] = []
    notes: list[str] = []

    if not config.API_KEY:
        problems.append("API_KEY is empty, so the gateway is unauthenticated.")
    if config.MODEL.startswith("mock/"):
        problems.append(f"MODEL is {config.MODEL} — a synthetic backend. Never serve this.")
    if config.ADMIN_API_KEY:
        notes.append("ADMIN_API_KEY is set: an HTTP caller with it gets shell/run_code/email.")
    if config.FILES_UNRESTRICTED:
        notes.append("FILES_ROOT=/ — file reads are unconfined, and therefore host-gated.")
    if config.ALLOW_PRIVATE_URLS:
        notes.append("ALLOW_PRIVATE_URLS=true — tools may reach private addresses.")
    if config.CRON_IN_WEB and config.WORKERS > 1:
        problems.append(f"CRON_IN_WEB with WORKERS={config.WORKERS}: every worker runs its own "
                        f"scheduler, so each job fires {config.WORKERS} times. Use WORKERS=1.")

    agents_store.reset()
    agents = agents_store.store().all()
    if not agents:
        problems.append("No agents are defined.")
    elif config.DEFAULT_PROFILE not in agents:
        problems.append(f"DEFAULT_PROFILE={config.DEFAULT_PROFILE!r} is not a defined agent "
                        f"(have: {', '.join(sorted(agents))}).")
    for name, agent in agents.items():
        if not agent.prompt.strip():
            problems.append(f"agent {name!r} has an empty prompt.")

    if config.TOOLS_BROWSER:
        try:
            import playwright  # noqa: F401
        except ImportError:
            problems.append("TOOLS_BROWSER=true but playwright is not installed "
                            "(pip install 'proteus-gateway[browser]').")

    services: dict[str, str] = {}
    if config.DATABASE_URL:
        services["postgres"] = _probe_db()
    if config.REDIS_URL:
        services["redis"] = _probe_redis()
    if config.MEMORY_ENABLED and config.OLLAMA_URL:
        services["ollama"] = _probe_http(f"{config.OLLAMA_URL}/api/tags")
    for name, state in services.items():
        if state != "ok":
            notes.append(f"{name} is {state} — features needing it degrade rather than fail.")

    gateway = try_json(f"{base_url(None)}/healthz")
    if gateway is None:
        gw_state = "not running"
    else:
        gw_state = gateway.get("status", "?")
        if not (gateway.get("model_auth") or {}).get("ok", True):
            problems.append("model credentials are dead — every completion will fail.")

    data = {
        "version": version(),
        "model": config.MODEL,
        "api_key_set": bool(config.API_KEY),
        "capacity": f"{config.WORKERS} x {config.MAX_CONCURRENT_COMPLETIONS}",
        "agents": {n: len(toolsets.load_for(a.toolset)[0] or []) for n, a in sorted(agents.items())},
        "file_tools": len(declarative.describe()),
        "services": services,
        "gateway": gw_state,
        "notes": notes,
        "problems": problems,
    }

    def render(d):
        out.print(f"[bold]proteus {d['version']}[/]")
        out.print(f"  model    : {d['model']}")
        out.print(f"  API_KEY  : {'set' if d['api_key_set'] else '[red]EMPTY[/]'}")
        out.print(f"  capacity : {d['capacity']} concurrent completions")
        out.print(f"\n[bold]agents[/] ({len(d['agents'])})")
        for name, count in d["agents"].items():
            out.print(f"  {name:14} {count} tools")
        out.print(f"\n[bold]tools[/]   {d['file_tools']} file-defined")
        if d["services"]:
            out.print("\n[bold]services[/]")
            for name, state in d["services"].items():
                out.print(f"  {name:10} [{'green' if state == 'ok' else 'red'}]{state}[/]")
        out.print(f"\n[bold]gateway[/] {d['gateway']}")
        for note in d["notes"]:
            out.print(f"[dim]  note: {note}[/]")
        if d["problems"]:
            out.print("\n[bold yellow]problems[/]")
            for p in d["problems"]:
                out.print(f"  [yellow]![/] {p}")
        else:
            out.print("\n[green]no problems found[/]")

    emit(data, as_json, render)
    if problems:
        raise typer.Exit(1)


def _probe_db() -> str:
    from app import db

    async def go() -> str:
        try:
            return "ok" if await db.try_init_pool(quiet=True) else "unreachable"
        finally:
            await db.close_pool()

    try:
        return asyncio.run(go())
    except Exception as exc:
        return f"error: {type(exc).__name__}"


def _probe_redis() -> str:
    from app import redisc

    async def go() -> str:
        try:
            client = await redisc.get_redis()
            if client is None:
                return "not configured"
            await client.ping()
            return "ok"
        except Exception:
            return "unreachable"
        finally:
            await redisc.close_redis()

    try:
        return asyncio.run(go())
    except Exception:
        return "unreachable"


def _probe_http(url: str) -> str:
    import httpx

    try:
        return "ok" if httpx.get(url, timeout=3).status_code < 500 else "error"
    except Exception:
        return "unreachable"


def run() -> None:
    try:
        app()
    except KeyboardInterrupt:
        err.print("\n[dim]interrupted[/]")
        sys.exit(130)


if __name__ == "__main__":
    run()
