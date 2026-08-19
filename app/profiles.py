"""
Profile resolution: name -> (system prompt, tools, dispatch).

A request picks a profile by name (header X-Proteus-Profile, body.profile, or the
session_key prefix); channels use DEFAULT_PROFILE. This is how one deployment
serves a general assistant on one path and a domain agent on another.

Definitions come from `agents_store` — `agents/*.md` when present, otherwise the
older env-var layout. Nothing here knows which; that is the point of the store.

A profile picks persona and toolset. It does NOT confer privilege: host-access
tools are added only when the CALLER is trusted, which is why the cache is keyed
on `host_tools` as well as the name.
"""
from __future__ import annotations

from typing import Any

from . import agents_store, config, toolsets

# (name, host_tools) -> (prompt, tools, dispatch); also keyed on the store's
# version so editing an agent file takes effect without a restart.
_cache: dict[tuple[str, bool, float], tuple[str, list | None, Any]] = {}


def names() -> list[str]:
    return sorted(agents_store.store().all())


def get(name: str) -> agents_store.Agent | None:
    return agents_store.store().get(name)


def all_agents() -> dict[str, agents_store.Agent]:
    return agents_store.store().all()


def pick(name: str | None) -> agents_store.Agent | None:
    """The agent a request resolves to, or None when none are defined."""
    return _pick(name)


def _pick(name: str | None) -> agents_store.Agent | None:
    agents = agents_store.store().all()
    if not agents:
        return None
    for candidate in (name, config.DEFAULT_PROFILE, "assistant"):
        if candidate and candidate in agents:
            return agents[candidate]
    return next(iter(agents.values()))          # last resort: any defined agent


def resolve(name: str | None, host_tools: bool = False):
    """Return (system_prompt_text, tools, dispatch) for a profile name."""
    agent = _pick(name)
    if agent is None:
        # No agents defined anywhere. Serve plain chat rather than failing the
        # request: a gateway with no persona is still a working gateway.
        tools, dispatch = toolsets.load_for(config.TOOLSET, host_tools=host_tools)
        return "", tools, dispatch

    key = (agent.name, host_tools, agents_store.store().version())
    hit = _cache.get(key)
    if hit is None:
        tools, dispatch = toolsets.load_for(agent.toolset, host_tools=host_tools)
        hit = (agent.prompt, tools, dispatch)
        _cache[key] = hit
        # Bound the cache: entries are keyed on a store version that changes on
        # every edit, so without this a long-lived process would accumulate one
        # set per edit.
        if len(_cache) > 64:
            for stale in [k for k in _cache if k[2] != key[2]]:
                _cache.pop(stale, None)
    return hit
