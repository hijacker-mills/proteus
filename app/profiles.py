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

from typing import Any, NamedTuple

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


class ModelChoice(NamedTuple):
    """Which model answers for a profile, and what to try if it fails."""
    model: str
    fallbacks: list[str]
    max_tokens: int
    reasoning_max_tokens: int | None = None
    reasoning_effort: str | None = None

    @property
    def chain(self) -> list[str]:
        return [self.model, *self.fallbacks]


def model_for(name: str | None) -> ModelChoice:
    """The model settings for a profile, falling back to the global config.

    Separate from `resolve()` on purpose. `resolve()` answers "what does this
    agent say and what can it do", which is cached per (name, host_tools); this
    answers "what runs it", which is cheap and needs no cache.

    Until this existed, `model:` and `max_tokens:` in agent frontmatter were
    parsed by `agents_store`, printed by `proteus agent list`, and then ignored:
    `agent.run` always called the global MODEL. An agent file could ask for a
    model and be silently overruled.

    Never raises. A gateway that cannot read an agent file should answer on the
    default model, not 500.
    """
    try:
        agent = _pick(name)
    except Exception:                            # pragma: no cover - defensive
        agent = None
    if agent is None:
        return ModelChoice(config.MODEL, [], config.MAX_TOKENS,
                           config.REASONING_MAX_TOKENS or None, None)
    # effort and max_tokens are mutually exclusive on OpenRouter; effort wins
    # when the agent asks for both.
    effort = agent.reasoning_effort
    return ModelChoice(
        agent.model or config.MODEL,
        list(agent.fallbacks),
        agent.max_tokens or config.MAX_TOKENS,
        None if effort else (agent.reasoning_max_tokens
                             or config.REASONING_MAX_TOKENS or None),
        effort,
    )


def tool_budget_for(name: str | None) -> tuple[int, int]:
    """(tool rounds, seconds) one reply may spend calling tools for a profile.

    The seconds are 0 when there is no time limit. Never raises, for the same
    reason as `model_for`.
    """
    try:
        agent = _pick(name)
    except Exception:                            # pragma: no cover - defensive
        agent = None
    rounds = (agent.tool_rounds if agent else None) or config.MAX_TOOL_TURNS
    seconds = (agent.tool_time_budget if agent else None) or config.TOOL_TIME_BUDGET
    return max(1, rounds), max(0, seconds)


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
