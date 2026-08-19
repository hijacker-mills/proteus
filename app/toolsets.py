"""
Composable toolsets. `config.TOOLSET` is a comma-separated list — the gateway
merges the tools from each and routes every tool call to the toolset that owns it.

  "none"    → no tools (plain conversational agent)
  "basics"  → datetime_now, calculate, remember, recall, todo  (no host access)
  "files"   → read_file, list_files   (confined to FILES_ROOT; no host access)
  "web"     → web_search, web_fetch, browser              (no host access)
  "agent"   → the web tools + run_code, shell, email, schedule (each gated)
  "custom"  → your own: tools/*.md and app/tools/custom/*.py

e.g. `toolset: [web, custom]` exposes both. Add a built-in toolset by
registering its (schemas, dispatch) in _provider(); add your own tools as files
and they appear under "custom" with no code change.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from . import config

DispatchFn = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]

# Tools that act on the HOST rather than on the conversation: arbitrary command
# execution, arbitrary Python, sending mail as the operator, and scheduling work
# that later runs with those same privileges. They exist for the operator's own
# allowlisted Telegram bot. They are withheld unless the caller is explicitly
# trusted (`host_tools=True`), so selecting a profile is never enough to reach
# them — no profile, expected or otherwise, grants host access by default.
_BASE_HOST_TOOLS = frozenset({"shell", "run_code", "email", "schedule"})

# Reading files is host access ONLY when it is unconfined. Confined to a
# FILES_ROOT the tools can reach nothing sensitive, so any agent may have them;
# unconfined they can read `.env`, `~/.ssh/id_rsa` and the gateway's own OAuth
# tokens, which is the same blast radius as `shell` and gets the same gate.
# Computed rather than constant, because that gate depends on configuration.


def host_tools() -> frozenset[str]:
    if config.FILES_UNRESTRICTED:
        return _BASE_HOST_TOOLS | {"read_file", "list_files"}
    return _BASE_HOST_TOOLS


class _HostTools(frozenset):
    """Keeps `HOST_TOOLS` usable as a set while staying config-aware."""

    def __contains__(self, item: object) -> bool:
        return item in host_tools()

    def __iter__(self):
        return iter(host_tools())

    def __len__(self) -> int:
        return len(host_tools())

    def __and__(self, other):
        return host_tools() & other

    def __rand__(self, other):
        return other & host_tools()


HOST_TOOLS = _HostTools()


async def _no_dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"error": "no tools enabled"}


def _provider(name: str) -> tuple[list[dict], DispatchFn] | None:
    if name == "files":
        # Read-only, and confined to FILES_ROOT — see app/tools/files.py for why
        # that keeps it off the host-tool list.
        from .tools import files
        return files.TOOLS, files.dispatch
    if name == "basics":
        # Small things models get wrong unaided: the date, arithmetic, and
        # remembering anything between conversations.
        from .tools import basics
        return basics.TOOLS, basics.dispatch
    if name == "agent":
        from .tools import agent_toolset
        return agent_toolset.TOOLS, agent_toolset.dispatch
    if name == "web":
        # Safe research subset of the agent toolset (no host access: no shell/run_code/email).
        # Suitable for agents exposed to untrusted users.
        from .tools import agent_toolset
        keep = {"web_search", "web_fetch", "browser"}
        tools = [t for t in (agent_toolset.TOOLS or []) if t["function"]["name"] in keep]
        return tools, agent_toolset.dispatch
    if name == "custom":
        # File-defined tools: tools/*.md (HTTP) and app/tools/custom/*.py.
        # Never host tools — see declarative.py for why.
        from .tools import declarative
        return declarative.load()
    return None


def load() -> tuple[list[dict] | None, DispatchFn]:
    return load_for(config.TOOLSET)


def load_for(toolset_str: str, host_tools: bool = False) -> tuple[list[dict] | None, DispatchFn]:
    """Build (schemas, dispatch) for a toolset string.

    host_tools=False (the default, and what every untrusted caller gets) strips
    HOST_TOOLS from the schemas AND refuses them at dispatch, so a model that
    hallucinates the tool name can't reach one either.
    """
    names = [n.strip() for n in (toolset_str or "").split(",") if n.strip() and n.strip() != "none"]

    merged: list[dict] = []
    routing: dict[str, DispatchFn] = {}
    seen: set[str] = set()
    for name in names:
        provider = _provider(name)
        if not provider:
            continue
        tools, dispatch = provider
        for t in tools or []:
            tname = t["function"]["name"]
            if tname in seen:
                continue
            if tname in HOST_TOOLS and not host_tools:
                continue
            seen.add(tname)
            merged.append(t)
            routing[tname] = dispatch

    if not merged:
        return None, _no_dispatch

    async def combined(tool_name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name in HOST_TOOLS and not host_tools:
            return {"error": f"{tool_name} is not available to this caller"}
        dispatch = routing.get(tool_name)
        if dispatch is None:
            return {"error": f"unknown tool: {tool_name}"}
        return await dispatch(tool_name, user_id, args)

    return merged, combined


TOOLS, DISPATCH = load()
