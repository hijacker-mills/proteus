"""
Composable toolsets. `config.TOOLSET` is a comma-separated list — the gateway
merges the tools from each and routes every tool call to the toolset that owns it.

  "none"     → no tools (plain conversational assistant)
  "agent"    → web_search, web_fetch, browser, run_code, shell, email
  "intelliq" → IntelliQ study tools (lecture_evidence, quiz_generate, memory_*, …)

e.g. TOOLSET=agent,intelliq exposes both. Add a toolset by registering its
(schemas, dispatch) in _provider().
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from . import config

DispatchFn = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def _no_dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"error": "no tools enabled"}


def _provider(name: str) -> tuple[list[dict], DispatchFn] | None:
    if name == "intelliq":
        from .schemas import TOOLS
        from .tools import dispatch
        return TOOLS, dispatch
    if name == "agent":
        from .tools import agent_toolset
        return agent_toolset.TOOLS, agent_toolset.dispatch
    if name == "web":
        # Safe research subset of the agent toolset (no host access: no shell/run_code/email).
        # Suitable for public profiles like Qubi.
        from .tools import agent_toolset
        keep = {"web_search", "web_fetch", "browser"}
        tools = [t for t in (agent_toolset.TOOLS or []) if t["function"]["name"] in keep]
        return tools, agent_toolset.dispatch
    return None


def load() -> tuple[list[dict] | None, DispatchFn]:
    return load_for(config.TOOLSET)


def load_for(toolset_str: str) -> tuple[list[dict] | None, DispatchFn]:
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
            seen.add(tname)
            merged.append(t)
            routing[tname] = dispatch

    if not merged:
        return None, _no_dispatch

    async def combined(tool_name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
        dispatch = routing.get(tool_name)
        if dispatch is None:
            return {"error": f"unknown tool: {tool_name}"}
        return await dispatch(tool_name, user_id, args)

    return merged, combined


TOOLS, DISPATCH = load()
