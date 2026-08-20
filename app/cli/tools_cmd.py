"""`proteus tool …` — list, scaffold and invoke tools."""
from __future__ import annotations

import asyncio
import json

import typer

from ._common import check_name, die, emit, err, load_env, out, table

app = typer.Typer(no_args_is_help=True, help="Create and test tools.")

BUILTIN = ("basics", "files", "web", "agent", "custom")

HTTP_TOOL = """---
name: {name}
method: GET
url: https://api.example.com/{name}
# auth: bearer ${{{upper}_API_KEY}}    # ${{VAR}} is read from the environment
# headers:
#   Accept: application/json
# send_user_header: x-user-id          # pass the gateway's user id to your backend
query:
  q: "{{{{query}}}}"                   # {{{{name}}}} is filled from the model's arguments
params:
  query: {{type: string, required: true, description: What to look up}}
---

Describe what this tool does and when to use it. The model reads this text and
decides from it whether to call the tool, so write it for the model, not for a
maintainer.
"""

PY_TOOL = '''"""Custom tool: {name}."""

SCHEMA = {{
    "type": "function",
    "function": {{
        "name": "{name}",
        "description": "Say what this does and when to use it — the model reads this.",
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
    """`user_id` comes from the authenticated request — never trust it from `args`.

    Return a dict. Raising is safe (the loop reports it), but returning
    {{"error": "..."}} gives the model something it can act on.
    """
    return {{"ok": True, "echo": args.get("query")}}
'''


@app.command("list")
def list_tools(as_json: bool = typer.Option(False, "--json")) -> None:
    """Show file-defined tools and what each built-in toolset contains."""
    load_env()
    from app import toolsets
    from app.tools import declarative

    files = declarative.describe()
    sets = {}
    for name in BUILTIN:
        tools, _ = toolsets.load_for(name, host_tools=True)
        sets[name] = [t["function"]["name"] for t in (tools or [])]
    data = {"file_defined": files, "toolsets": sets,
            "host_tools": sorted(toolsets.host_tools())}

    def render(d):
        if d["file_defined"]:
            t = table("kind", "name", "toolset", "target", "source")
            for r in d["file_defined"]:
                t.add_row(r["kind"], f"[bold]{r['name']}[/]", r.get("toolset", "custom"),
                          r["target"][:52], r["source"])
            out.print(t)
        else:
            out.print("[yellow]No file-defined tools.[/] Add one: proteus tool new <name> --http")
        out.print("\n[dim]toolsets[/]")
        for name, names in d["toolsets"].items():
            out.print(f"  {name:8} {', '.join(names) or '-'}")
        out.print(f"\n[dim]host tools (withheld unless the caller is trusted):[/] "
                  f"{', '.join(d['host_tools'])}")

    emit(data, as_json, render)


@app.command("new")
def new(name: str,
        http: bool = typer.Option(False, "--http", help="Declarative HTTP tool (no code)."),
        python: bool = typer.Option(False, "--python", help="Python handler stub.")) -> None:
    """Scaffold a tool, declarative or Python."""
    name = check_name(name, "tool")
    if http == python:
        die("choose exactly one of --http or --python",
            "--http for an API call, --python when it needs real logic.")

    load_env()
    from app import toolsets
    from app.tools import declarative

    if name in toolsets.host_tools():
        die(f"{name!r} is the name of a host tool", "Pick another name; it would never be reachable.")

    if http:
        path = declarative.TOOLS_DIR / f"{name}.md"
        body = HTTP_TOOL.format(name=name, upper=name.upper())
    else:
        path = declarative.CUSTOM_DIR / f"{name}.py"
        body = PY_TOOL.format(name=name)
    if path.exists():
        die(f"{path} already exists")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        die(f"could not write {path}: {exc.strerror or exc}")

    out.print(f"[green]created[/] {path}")
    out.print(f"[dim]Add [bold]custom[/bold] to an agent's toolset, then:[/] "
              f"proteus tool test {name} --args '{{\"query\":\"hi\"}}'")


@app.command("test")
def test(name: str,
         args: str = typer.Option("{}", "--args", "-a", help="JSON arguments."),
         toolset: str = typer.Option(None, "--toolset",
                                     help="Which toolset to load. Default: search them all."),
         user: str = typer.Option("cli-user", "--user",
                                  help="user_id the tool is scoped to, as the gateway would supply."),
         as_json: bool = typer.Option(False, "--json")) -> None:
    """Invoke a tool directly, exactly as the agent loop would.

    Searches every toolset, not just the file-defined ones, so built-ins like
    `calculate` and `web_search` are testable too.
    """
    load_env()
    from app import toolsets

    try:
        parsed = json.loads(args)
    except json.JSONDecodeError as exc:
        die(f"--args is not valid JSON: {exc}", """Example: --args '{"query":"hello"}'""")
    if not isinstance(parsed, dict):
        die("--args must be a JSON object", """Example: --args '{"query":"hello"}'""")

    candidates = [toolset] if toolset else list(BUILTIN)
    found = None
    for ts in candidates:
        tools, dispatch = toolsets.load_for(ts, host_tools=True)
        if any(t["function"]["name"] == name for t in (tools or [])):
            found = (ts, dispatch)
            break

    if found is None:
        available = sorted({t["function"]["name"]
                            for ts in BUILTIN
                            for t in (toolsets.load_for(ts, host_tools=True)[0] or [])})
        die(f"no tool named {name!r}", f"Available: {', '.join(available) or 'none'}")

    ts, dispatch = found
    # Diagnostics to stderr, the result to stdout — so `--json | jq` is clean.
    if not as_json:
        err.print(f"[dim]{name} (from the {ts} toolset), as user {user!r}[/]")
    try:
        result = asyncio.run(dispatch(name, user, parsed))
    except Exception as exc:                      # a broken tool must not traceback at the user
        die(f"the tool raised {type(exc).__name__}", str(exc)[:300])

    print(json.dumps(result, default=str, indent=2))
    if isinstance(result, dict) and "error" in result:
        raise typer.Exit(1)
