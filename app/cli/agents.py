"""`proteus agent …` — create, inspect, validate and remove agents."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer

from ._common import (check_name, confirm_tty, die, emit, err, load_env, out,
                      table)

app = typer.Typer(no_args_is_help=True, help="Create and inspect agents.")

TEMPLATE = """---
name: {name}
description: {description}
toolset: [{toolset}]
# model: anthropic/claude-sonnet-4-6   # omit to inherit MODEL
# modes:
#   terse: |
#     Answer in at most three sentences. No preamble.
---

You are {name}.

Replace this with the agent's real persona. Everything below the frontmatter is
sent to the model as its system prompt, so write it as prose, not config.
"""


def _store():
    load_env()
    from app import agents_store

    agents_store.reset()          # always read from disk; the CLI is short-lived
    return agents_store


def _path_for(store, name: str) -> Path:
    """The file backing an agent — which may belong to a mounted pack rather
    than this deployment's own directory. Falls back to where a file of that
    name WOULD go, so callers can report a sensible "does not exist"."""
    agent = store.store().get(name)
    if agent and agent.source.startswith("file:"):
        return Path(agent.source[len("file:"):])
    return store.AGENTS_DIR / f"{name}.md"


def _describe(agent, with_tools: bool = True) -> dict:
    from app import toolsets

    row = {
        "name": agent.name,
        "description": agent.description,
        "toolset": agent.toolset,
        "model": agent.model,
        "max_tokens": agent.max_tokens,
        "modes": sorted(agent.modes),
        "prompt_chars": len(agent.prompt),
        "source": agent.source,
    }
    if with_tools:
        tools, _ = toolsets.load_for(agent.toolset)
        row["tools"] = [t["function"]["name"] for t in (tools or [])]
    return row


@app.command("list")
def list_agents(as_json: bool = typer.Option(False, "--json", help="Machine-readable output.")) -> None:
    """Show every defined agent."""
    agents = _store().store().all()
    rows = [_describe(a) for _, a in sorted(agents.items())]

    def render(rows):
        if not rows:
            out.print("[yellow]No agents defined.[/] Create one: proteus agent new <name>")
            return
        t = table("name", "toolset", "tools", "prompt", "source")
        for r in rows:
            t.add_row(f"[bold]{r['name']}[/]", r["toolset"] or "none",
                      str(len(r["tools"])), f"{r['prompt_chars']} ch", r["source"])
        out.print(t)

    emit(rows, as_json, render)


@app.command("show")
def show(name: str,
         prompt: bool = typer.Option(False, "--prompt", help="Print the whole system prompt."),
         as_json: bool = typer.Option(False, "--json")) -> None:
    """Show one agent in full."""
    agent = _store().store().get(name)
    if agent is None:
        known = ", ".join(sorted(_store().store().all())) or "none"
        die(f"no agent named {name!r}", f"Known agents: {known}")

    data = _describe(agent)
    if prompt:
        data["prompt"] = agent.prompt

    def render(d):
        out.print(f"[bold]{d['name']}[/]  [dim]{d['source']}[/]")
        out.print(f"  description : {d['description'] or '-'}")
        out.print(f"  toolset     : {d['toolset'] or 'none'}")
        out.print(f"  tools ({len(d['tools']):2}) : {', '.join(d['tools']) or '-'}")
        out.print(f"  model       : {d['model'] or 'inherit MODEL'}")
        out.print(f"  modes       : {', '.join(d['modes']) or '-'}")
        out.print(f"  prompt      : {d['prompt_chars']} chars (~{d['prompt_chars'] // 4} tokens)")
        if prompt:
            out.print("\n[dim]─── system prompt ───[/]")
            out.print(d["prompt"])

    emit(data, as_json, render)


@app.command("new")
def new(name: str,
        toolset: str = typer.Option("basics", "--toolset", "-t",
                                    help="Comma list: none, basics, files, web, agent, custom"),
        description: str = typer.Option("", "--description", "-d"),
        edit_now: bool = typer.Option(False, "--edit", help="Open it in $EDITOR afterwards.")) -> None:
    """Create agents/<name>.md."""
    name = check_name(name, "agent")
    store = _store()
    path = store.AGENTS_DIR / f"{name}.md"
    if path.exists():
        die(f"{path} already exists", f"Edit it:  proteus agent edit {name}")

    from app import toolsets

    wanted = [t.strip() for t in toolset.split(",") if t.strip()]
    unknown = [t for t in wanted if t != "none" and toolsets.load_for(t)[0] is None]
    if unknown:
        err.print(f"[yellow]warning:[/] toolset(s) {', '.join(unknown)} resolve to no tools right now")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE.format(name=name,
                                        description=description or f"The {name} agent",
                                        toolset=", ".join(wanted) or "none"), encoding="utf-8")
    except OSError as exc:
        die(f"could not write {path}: {exc.strerror or exc}")

    out.print(f"[green]created[/] {path}")
    out.print("[dim]Edit the body to set its persona. The gateway picks it up without a restart.[/]")
    if edit_now:
        edit(name)


@app.command("edit")
def edit(name: str) -> None:
    """Open agents/<name>.md in $EDITOR, then validate it."""
    store = _store()
    path = _path_for(store, name)
    if not path.exists():
        die(f"{path} does not exist", f"Create it:  proteus agent new {name}")

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
    try:
        subprocess.call([editor, str(path)])
    except FileNotFoundError:
        die(f"editor {editor!r} not found", "Set $EDITOR to one you have installed.")
    validate(name)


@app.command("validate")
def validate(name: str = typer.Argument(None, help="Agent to check; omit for all.")) -> None:
    """Check that agents parse and their toolsets resolve. Exits non-zero on any problem."""
    store = _store()
    agents = store.store().all()
    targets = {name: agents[name]} if name and name in agents else agents
    if name and name not in agents:
        die(f"no agent named {name!r}")
    if not targets:
        die("no agents defined")

    from app import toolsets

    problems = 0
    for agent_name, agent in sorted(targets.items()):
        issues = []
        if not agent.prompt.strip():
            issues.append("empty prompt body")
        if len(agent.prompt) < 20:
            issues.append("prompt looks like the unedited template")
        tools, _ = toolsets.load_for(agent.toolset)
        if agent.toolset not in ("", "none") and not tools:
            issues.append(f"toolset {agent.toolset!r} resolves to no tools")
        for mode, block in agent.modes.items():
            if not block.strip():
                issues.append(f"mode {mode!r} is empty")
        if agent.extra:
            issues.append(f"unrecognised frontmatter keys: {', '.join(sorted(agent.extra))}")

        if issues:
            problems += 1
            out.print(f"[red]✗[/] {agent_name}")
            for i in issues:
                out.print(f"    {i}")
        else:
            out.print(f"[green]✓[/] {agent_name}  ({len(tools or [])} tools)")

    if problems:
        raise typer.Exit(1)


@app.command("rm")
def rm(name: str, yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete agents/<name>.md."""
    store = _store()
    path = _path_for(store, check_name(name, "agent"))
    if not path.exists():
        die(f"{path} does not exist")
    if path.parent != store.AGENTS_DIR:
        err.print(f"[yellow]warning:[/] {name} belongs to a mounted pack ({path.parent}), "
                  f"not this deployment")
    if not yes and not confirm_tty(f"Delete {path}?"):
        raise typer.Exit(1)
    path.unlink()
    out.print(f"[green]deleted[/] {path}")
