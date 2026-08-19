"""proteus CLI — agents, tools, and a running gateway."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Many agents, one gateway. Manage proteus agents, tools and a running instance.")
agent_app = typer.Typer(no_args_is_help=True, help="Create and inspect agents.")
tool_app = typer.Typer(no_args_is_help=True, help="Create and test tools.")
app.add_typer(agent_app, name="agent")
app.add_typer(tool_app, name="tool")

con = Console()
REPO = Path(__file__).resolve().parent.parent.parent


def _load_env() -> None:
    """Load .env with python-dotenv — the SAME loader app/config.py uses.

    Hand-rolling this is a trap: a line like `TOOLS_SHELL=true  # locked down`
    parses to the string "true  # locked down" under a naive split, which is
    truthy-looking but fails every `== "true"` check, so the CLI silently
    reports a different toolset than the server actually serves.
    """
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")


def _base(remote: str | None) -> str:
    return (remote or f"http://127.0.0.1:{os.environ.get('PORT', '18791')}").rstrip("/")


def _get(url: str) -> dict:
    import httpx
    r = httpx.get(url, timeout=15)
    r.raise_for_status()
    return r.json()


# ── agents ───────────────────────────────────────────────────────────────────

@agent_app.command("list")
def agent_list() -> None:
    """Show every defined agent and where it came from."""
    _load_env()
    from app import agents_store
    agents = agents_store.store().all()
    if not agents:
        con.print("[yellow]No agents defined.[/] Create one with `proteus agent new <name>`.")
        raise typer.Exit(0)
    t = Table("name", "toolset", "prompt", "source", "description", box=None, pad_edge=False)
    for name, a in sorted(agents.items()):
        t.add_row(f"[bold]{name}[/]", a.toolset or "none",
                  f"{len(a.prompt)} ch", a.source, (a.description or "")[:52])
    con.print(t)


@agent_app.command("show")
def agent_show(name: str, prompt: bool = typer.Option(False, "--prompt", help="Print the full prompt.")) -> None:
    """Show one agent's configuration, and optionally its whole prompt."""
    _load_env()
    from app import agents_store, toolsets
    a = agents_store.store().get(name)
    if a is None:
        con.print(f"[red]No agent named {name!r}.[/] Known: {', '.join(sorted(agents_store.store().all())) or 'none'}")
        raise typer.Exit(1)
    tools, _ = toolsets.load_for(a.toolset)
    con.print(f"[bold]{a.name}[/]  [dim]{a.source}[/]")
    con.print(f"  description : {a.description or '-'}")
    con.print(f"  toolset     : {a.toolset or 'none'}  "
              f"({len(tools or [])} tools: {', '.join(t['function']['name'] for t in (tools or [])) or '-'})")
    con.print(f"  model       : {a.model or 'inherit MODEL'}")
    con.print(f"  prompt      : {len(a.prompt)} chars (~{len(a.prompt)//4} tokens)")
    if a.extra:
        con.print(f"  extra       : {a.extra}")
    if prompt:
        con.print("\n[dim]---[/]\n" + a.prompt)


_AGENT_TEMPLATE = """---
name: {name}
description: {description}
toolset: [{toolset}]
---

You are {name}, a helpful assistant.

Replace this with the agent's real persona. Everything below the frontmatter is
sent to the model as the system prompt, so write it as prose, not config.
"""


@agent_app.command("new")
def agent_new(
    name: str,
    toolset: str = typer.Option("none", "--toolset", help="Comma list: none, web, agent, custom"),
    description: str = typer.Option("", "--description"),
) -> None:
    """Create agents/<name>.md."""
    _load_env()
    from app import agents_store
    path = agents_store.AGENTS_DIR / f"{name}.md"
    if path.exists():
        con.print(f"[red]{path} already exists.[/]")
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_AGENT_TEMPLATE.format(
        name=name, description=description or f"The {name} agent",
        toolset=", ".join(t.strip() for t in toolset.split(",") if t.strip()) or "none",
    ), encoding="utf-8")
    con.print(f"[green]created[/] {path}")
    con.print("Edit the body to set its persona. The gateway picks it up without a restart.")


@agent_app.command("edit")
def agent_edit(name: str) -> None:
    """Open agents/<name>.md in $EDITOR."""
    _load_env()
    from app import agents_store
    path = agents_store.AGENTS_DIR / f"{name}.md"
    if not path.exists():
        con.print(f"[red]{path} does not exist.[/] Create it with `proteus agent new {name}`.")
        raise typer.Exit(1)
    subprocess.call([os.environ.get("EDITOR", "nano"), str(path)])


@agent_app.command("rm")
def agent_rm(name: str, yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete agents/<name>.md."""
    _load_env()
    from app import agents_store
    path = agents_store.AGENTS_DIR / f"{name}.md"
    if not path.exists():
        con.print(f"[red]{path} does not exist.[/]")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Delete {path}?"):
        raise typer.Exit(1)
    path.unlink()
    con.print(f"[green]deleted[/] {path}")


# ── tools ────────────────────────────────────────────────────────────────────

@tool_app.command("list")
def tool_list() -> None:
    """Show file-defined tools, and the built-in toolsets."""
    _load_env()
    from app import toolsets
    from app.tools import declarative
    rows = declarative.describe()
    if rows:
        t = Table("kind", "name", "target", "source", box=None, pad_edge=False)
        for r in rows:
            t.add_row(r["kind"], f"[bold]{r['name']}[/]", r["target"][:52], r["source"])
        con.print(t)
    else:
        con.print("[yellow]No file-defined tools.[/] Add one with `proteus tool new <name> --http`.")
    con.print("\n[dim]built-in toolsets[/]")
    for name in ("basics", "files", "web", "agent", "custom"):
        tools, _ = toolsets.load_for(name, host_tools=True)
        con.print(f"  {name:9} {', '.join(t['function']['name'] for t in (tools or [])) or '-'}")


_HTTP_TOOL = """---
name: {name}
method: GET
url: https://api.example.com/{name}
# auth: bearer ${{{upper}_API_KEY}}      # ${{VAR}} is read from the environment
query:
  q: "{{{{query}}}}"                     # {{{{name}}}} is filled from the model's arguments
params:
  query: {{type: string, required: true, description: What to look up}}
---

Describe what this tool does and when to use it. The model reads this text and
decides from it whether to call the tool, so write it for the model.
"""

_PY_TOOL = '''"""Custom tool: {name}."""

SCHEMA = {{
    "type": "function",
    "function": {{
        "name": "{name}",
        "description": "Describe what this does; the model reads this to decide when to call it.",
        "parameters": {{
            "type": "object",
            "properties": {{
                "query": {{"type": "string", "description": "What to look up"}},
            }},
            "required": ["query"],
        }},
    }},
}}


async def handler(user_id: str, args: dict) -> dict:
    """`user_id` is supplied by the gateway — never trust it from `args`."""
    return {{"ok": True, "echo": args.get("query")}}
'''


@tool_app.command("new")
def tool_new(
    name: str,
    http: bool = typer.Option(False, "--http", help="Declarative HTTP tool (no code)."),
    python: bool = typer.Option(False, "--python", help="Python handler stub."),
) -> None:
    """Scaffold a new tool, either declarative or Python."""
    _load_env()
    from app.tools import declarative
    if http == python:
        con.print("[red]Choose exactly one of --http or --python.[/]")
        raise typer.Exit(1)
    if http:
        path = declarative.TOOLS_DIR / f"{name}.md"
        body = _HTTP_TOOL.format(name=name, upper=name.upper())
    else:
        path = declarative.CUSTOM_DIR / f"{name}.py"
        body = _PY_TOOL.format(name=name)
    if path.exists():
        con.print(f"[red]{path} already exists.[/]")
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    con.print(f"[green]created[/] {path}")
    con.print("Add [bold]custom[/] to an agent's toolset to expose it, "
              "then `proteus tool test " + name + " --args '{\"query\":\"hi\"}'`.")


@tool_app.command("test")
def tool_test(name: str, args: str = typer.Option("{}", "--args", help="JSON arguments.")) -> None:
    """Invoke a tool directly, exactly as the agent loop would."""
    _load_env()
    import asyncio
    from app import toolsets
    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as exc:
        con.print(f"[red]--args is not valid JSON:[/] {exc}")
        raise typer.Exit(1)
    tools, dispatch = toolsets.load_for("custom")
    known = {t["function"]["name"] for t in (tools or [])}
    if name not in known:
        con.print(f"[red]No file-defined tool named {name!r}.[/] Known: {', '.join(sorted(known)) or 'none'}")
        raise typer.Exit(1)
    out = asyncio.run(dispatch(name, "cli-user", parsed))
    con.print_json(json.dumps(out, default=str))


# ── the running gateway ──────────────────────────────────────────────────────

@app.command()
def health(remote: str = typer.Option(None, "--remote", help="Gateway URL.")) -> None:
    """Show /healthz, formatted."""
    _load_env()
    try:
        h = _get(f"{_base(remote)}/healthz")
    except Exception as exc:
        con.print(f"[red]unreachable[/] {_base(remote)}: {exc}")
        raise typer.Exit(1)
    colour = {"ok": "green", "degraded": "yellow", "down": "red"}.get(h.get("status"), "white")
    con.print(f"[{colour}]{h.get('status','?').upper()}[/]  {_base(remote)}")
    con.print(f"  model      : {h.get('model')}")
    con.print(f"  database   : {'connected' if h.get('db') else 'NOT connected (chat still works)'}")
    con.print(f"  toolset    : {h.get('toolset')}")
    c = h.get("concurrency") or {}
    con.print(f"  in flight  : {c.get('in_use')}/{c.get('limit')} per worker  (rejected: {c.get('rejected')})")
    con.print(f"  channels   : {', '.join(h.get('channels') or []) or 'none'}")
    auth = h.get("model_auth") or {}
    if auth.get("detail"):
        con.print(f"  [yellow]{auth['detail']}[/]")


@app.command()
def serve(
    host: str = typer.Option(None, "--host"),
    port: int = typer.Option(None, "--port"),
    workers: int = typer.Option(None, "--workers"),
) -> None:
    """Run the gateway (what scripts/run.sh does)."""
    _load_env()
    cmd = [sys.executable, "-m", "uvicorn", "app.main:app",
           "--host", host or os.environ.get("HOST", "0.0.0.0"),
           "--port", str(port or os.environ.get("PORT", "18791")),
           "--workers", str(workers or os.environ.get("WORKERS", "4")),
           "--loop", "uvloop", "--http", "httptools",
           "--no-access-log", "--timeout-keep-alive", "75"]
    os.chdir(REPO)
    raise typer.Exit(subprocess.call(cmd))


@app.command()
def chat(
    agent: str = typer.Argument(None, help="Agent (profile) to talk to."),
    remote: str = typer.Option(None, "--remote"),
    user: str = typer.Option("cli-user", "--user", help="user_id every tool call is scoped to."),
) -> None:
    """Interactive REPL against a running gateway. Ctrl-C to leave."""
    _load_env()
    import httpx
    base, key = _base(remote), os.environ.get("API_KEY", "")
    hdr = {"Authorization": f"Bearer {key}", "X-Proteus-User-Id": user}
    if agent:
        hdr["X-Proteus-Profile"] = agent
    history: list[dict] = []
    con.print(f"[dim]{base}  agent={agent or 'default'}  user={user}[/]")
    with httpx.Client(timeout=300) as c:
        while True:
            try:
                msg = typer.prompt("\nyou")
            except (KeyboardInterrupt, EOFError):
                con.print("\n[dim]bye[/]"); return
            history.append({"role": "user", "content": msg})
            con.print("[bold]proteus[/] ", end="")
            reply = []
            try:
                with c.stream("POST", f"{base}/v1/chat/completions", headers=hdr,
                              json={"stream": True, "messages": history}) as r:
                    if r.status_code != 200:
                        con.print(f"[red]HTTP {r.status_code}[/] {r.read()[:200]!r}"); history.pop(); continue
                    for line in r.iter_lines():
                        if not line.startswith("data:") or line == "data: [DONE]":
                            continue
                        d = json.loads(line[5:])
                        if "proteus_tool_event" in d:
                            e = d["proteus_tool_event"]
                            con.print(f"\n[dim]  ⚙ {e['tool']} {e['status']} {e['ms']}ms[/]")
                            continue
                        ct = d["choices"][0]["delta"].get("content")
                        if ct:
                            reply.append(ct); print(ct, end="", flush=True)
                print()
            except KeyboardInterrupt:
                con.print("\n[dim]interrupted[/]")
            history.append({"role": "assistant", "content": "".join(reply)})


@app.command()
def bench(
    concurrency: int = typer.Option(50, "--concurrency", "-c"),
    n: int = typer.Option(200, "-n"),
    remote: str = typer.Option(None, "--remote"),
) -> None:
    """Load-test a running gateway. Point MODEL at mock/* first, or it costs real tokens."""
    _load_env()
    script = REPO / "tests" / "loadtest.py"
    raise typer.Exit(subprocess.call(
        [sys.executable, str(script), "--n", str(n), "--concurrency", str(concurrency),
         "--base", _base(remote)]))


@app.command()
def login() -> None:
    """ChatGPT (Codex) OAuth device login, for MODEL=codex/*."""
    raise typer.Exit(subprocess.call(["bash", str(REPO / "scripts" / "codex_login.sh")]))


@app.command()
def doctor() -> None:
    """Check configuration and connectivity, and say what is wrong."""
    _load_env()
    from app import config
    problems: list[str] = []

    con.print("[bold]config[/]")
    con.print(f"  MODEL       : {config.MODEL}")
    if config.MODEL.startswith("mock/"):
        problems.append("MODEL is a mock backend — fine for load tests, never for production.")
    con.print(f"  API_KEY     : {'set' if config.API_KEY else 'EMPTY (open mode)'}")
    if not config.API_KEY:
        problems.append("API_KEY is empty, so the gateway is unauthenticated.")
    if config.ADMIN_API_KEY:
        problems.append("ADMIN_API_KEY is set — HTTP callers presenting it get shell/run_code/email.")
    con.print(f"  DATABASE_URL: {'set' if config.DATABASE_URL else 'empty (stateless: no memory/channels/cron)'}")
    con.print(f"  workers x limit: {config.WORKERS} x {config.MAX_CONCURRENT_COMPLETIONS} "
              f"= {config.WORKERS * config.MAX_CONCURRENT_COMPLETIONS} concurrent completions")

    from app import agents_store, toolsets
    from app.tools import declarative
    agents = agents_store.store().all()
    con.print("\n[bold]agents[/]")
    for name, a in sorted(agents.items()):
        tools, _ = toolsets.load_for(a.toolset)
        con.print(f"  {name:12} {len(tools or []):2} tools  {a.source}")
        if not a.prompt.strip():
            problems.append(f"agent {name} has an empty prompt.")
    if not agents:
        problems.append("No agents defined at all.")
    if config.DEFAULT_PROFILE not in agents and agents:
        problems.append(f"DEFAULT_PROFILE={config.DEFAULT_PROFILE!r} is not a defined agent.")

    rows = declarative.describe()
    con.print(f"\n[bold]file-defined tools[/] ({len(rows)})")
    for r in rows:
        con.print(f"  {r['kind']:7} {r['name']}")

    con.print("\n[bold]gateway[/]")
    try:
        h = _get(f"{_base(None)}/healthz")
        con.print(f"  {h['status']}  model={h['model']}  db={h['db']}")
        if not (h.get("model_auth") or {}).get("ok", True):
            problems.append("model credentials are dead — every completion will fail.")
    except Exception as exc:
        con.print(f"  [dim]not running ({type(exc).__name__})[/]")

    if problems:
        con.print("\n[bold yellow]issues[/]")
        for p in problems:
            con.print(f"  ! {p}")
        raise typer.Exit(1)
    con.print("\n[green]no issues found[/]")


def run() -> None:
    app()


if __name__ == "__main__":
    run()
