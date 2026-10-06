"""
Agent definitions: one Markdown file per agent, frontmatter for config.

An agent is a long prose prompt plus a little config. Markdown puts those the
right way round — the frontmatter carries the config, the body IS the prompt:

    ---
    name: support
    description: Answers product questions and files tickets
    toolset: [web, custom]
    model: null          # null / omitted = inherit MODEL
    fallbacks: []        # models to try, in order, if `model` fails
    max_tokens: 1500
    tool_rounds: 4       # omitted = MAX_TOOL_TURNS
    tool_time_budget: 45 # seconds into a reply before it must answer; omitted = TOOL_TIME_BUDGET
    ---

    You are a support agent for Acme…

The alternative (config file pointing at a separate prompt file) splits one
concept across two places, which is what `profiles._PROFILES` used to do: the
prompt path came from one env var and the toolset from another.

STORAGE. `AgentStore` is deliberately a seam. Files keep agents in git, keep
them reviewable, and keep chat free of a database dependency. When several
replicas need one source of truth, a Postgres-backed store slots in behind the
same three methods without touching callers.

FALLBACK. With no `agents/` directory at all, one agent is assembled from
SYSTEM_PROMPT_FILE + TOOLSET so the gateway still serves rather than refusing to
start.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import config

logger = logging.getLogger("proteus.agents")

_APP_DIR = Path(__file__).parent
_REPO_DIR = _APP_DIR.parent
# Where `proteus agent new` writes; AGENTS_DIRS is everywhere definitions are
# read from, this deployment's own directory first and mounted packs after.
AGENTS_DIR = config.AGENTS_DIR
AGENTS_DIRS = config.AGENTS_DIRS


@dataclass
class Agent:
    name: str
    prompt: str
    toolset: str = "none"
    description: str = ""
    model: str | None = None
    # Tried in order when `model` fails BEFORE producing any output. A model that
    # has already started streaming is never retried on another model — see
    # `llm.astream_chain`, which explains why.
    fallbacks: list[str] = field(default_factory=list)
    max_tokens: int | None = None
    # Hard cap on reasoning tokens for a forced-reasoning model, so thinking
    # cannot eat the whole `max_tokens` budget before any answer is emitted.
    # None means fall back to the gateway's REASONING_MAX_TOKENS.
    reasoning_max_tokens: int | None = None
    # OpenRouter/OpenAI reasoning effort (low|medium|high). When set it is sent
    # instead of reasoning_max_tokens — OpenRouter accepts only one of the two.
    reasoning_effort: str | None = None
    # Named behaviour blocks appended to the system prompt when a request asks
    # for one by name (X-Proteus-Mode). The gateway supplies the MECHANISM; the
    # agent supplies the CONTENT, so no domain vocabulary lives in the core.
    modes: dict[str, str] = field(default_factory=dict)
    # How many rounds of tool calls one reply may make, and how many seconds it
    # may run, before it stops calling tools and answers from what it has.
    # None means the gateway's MAX_TOOL_TURNS / TOOL_TIME_BUDGET.
    tool_rounds: int | None = None
    tool_time_budget: int | None = None
    source: str = "env"                       # where it came from, for `proteus agent list`
    extra: dict[str, Any] = field(default_factory=dict)

    def mode_block(self, name: str | None) -> str:
        """The block for an explicitly requested mode, or "" for none/unknown."""
        return self.modes.get((name or "").strip().lower(), "")


def parse_markdown(text: str, *, name_hint: str = "") -> tuple[dict[str, Any], str]:
    """Split `---\\nYAML\\n---\\nbody` into (frontmatter, body).

    A file with no frontmatter is treated as all-body, so a plain prompt file
    is a valid agent with default settings.
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        logger.warning("agent %s: bad frontmatter (%s) — treating file as prompt-only",
                       name_hint or "?", exc)
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, parts[2].lstrip("\n")


def _toolset_str(value: Any) -> str:
    """Accept `toolset: [web, custom]` or `toolset: web,custom`."""
    if value is None:
        return "none"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _str_list(value: Any) -> list[str]:
    """Accept `fallbacks: [a, b]` or `fallbacks: a,b`, and drop empties."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [s for s in (str(v).strip() for v in items) if s]


def _positive_int(meta: dict[str, Any], key: str, name: str) -> int | None:
    """A positive whole number from frontmatter, or None (logged) when it is not one."""
    if meta.get(key) is None:
        return None
    try:
        value = int(meta[key])
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        logger.warning("agent %s: invalid %s %r, ignoring", name or "?", key, meta[key])
        return None
    return value


def agent_from_markdown(text: str, name: str, source: str) -> Agent:
    meta, body = parse_markdown(text, name_hint=name)
    known = {"name", "description", "toolset", "model", "fallbacks", "max_tokens",
             "reasoning_max_tokens", "reasoning_effort", "modes", "tool_rounds",
             "tool_time_budget"}
    effort = str(meta.get("reasoning_effort") or "").strip().lower() or None
    if effort and effort not in {"minimal", "low", "medium", "high"}:
        logger.warning("agent %s: invalid reasoning_effort %r — ignoring",
                       name or "?", effort)
        effort = None
    return Agent(
        name=str(meta.get("name") or name).strip(),
        prompt=body.strip(),
        toolset=_toolset_str(meta.get("toolset")),
        description=str(meta.get("description") or "").strip(),
        model=(str(meta["model"]).strip() or None) if meta.get("model") else None,
        fallbacks=_str_list(meta.get("fallbacks")),
        max_tokens=int(meta["max_tokens"]) if meta.get("max_tokens") else None,
        reasoning_max_tokens=(int(meta["reasoning_max_tokens"])
                              if meta.get("reasoning_max_tokens") else None),
        reasoning_effort=effort,
        modes={str(k).strip().lower(): str(v).strip()
               for k, v in (meta.get("modes") or {}).items() if str(v).strip()},
        tool_rounds=_positive_int(meta, "tool_rounds", name),
        tool_time_budget=_positive_int(meta, "tool_time_budget", name),
        source=source,
        extra={k: v for k, v in meta.items() if k not in known},
    )


class AgentStore:
    """Interface. A Postgres-backed store implements these three and nothing else."""

    def all(self) -> dict[str, Agent]:
        raise NotImplementedError

    def get(self, name: str) -> Agent | None:
        return self.all().get(name)

    def version(self) -> float:
        """Any value that changes when definitions change; drives cache busting."""
        return 0.0


class EnvStore(AgentStore):
    """One agent assembled from environment variables, for a deployment with no
    `agents/` directory: SYSTEM_PROMPT_FILE + TOOLSET, named DEFAULT_PROFILE.

    Deliberately generic. Anything domain-specific is a file in `agents/`, so no
    product's vocabulary is hard-coded into the gateway.
    """

    def all(self) -> dict[str, Agent]:
        path = _APP_DIR / config.SYSTEM_PROMPT_FILE
        if not path.exists():
            return {}
        name = config.DEFAULT_PROFILE or "assistant"
        meta, body = parse_markdown(path.read_text(encoding="utf-8"), name_hint=name)
        return {name: Agent(name=name, prompt=body.strip(), toolset=config.TOOLSET,
                            description=str(meta.get("description") or ""),
                            source=f"env:{config.SYSTEM_PROMPT_FILE}")}


class FileStore(AgentStore):
    """agents/*.md on disk, re-read when a file changes.

    Reads from several directories — this deployment's own, then each mounted
    pack's — so an integration ships its agent without being merged into this
    repo. Earlier directories win a name clash, and the clash is logged: an
    agent silently replaced by one from a pack is a very confusing outage.
    """

    def __init__(self, directories: Path | list[Path]) -> None:
        self.dirs = [directories] if isinstance(directories, Path) else list(directories)
        self._cache: dict[str, Agent] = {}
        self._stamp: float = -1.0

    @property
    def dir(self) -> Path:
        """The primary directory — where new definitions are written."""
        return self.dirs[0]

    def _files(self) -> list[Path]:
        return [p for d in self.dirs if d.is_dir() for p in sorted(d.glob("*.md"))]

    def _mtime(self) -> float:
        files = self._files()
        if not files:
            return -1.0
        # Include the count so a deletion also busts the cache.
        return max(p.stat().st_mtime for p in files) + len(files)

    def version(self) -> float:
        return self._mtime()

    def all(self) -> dict[str, Agent]:
        stamp = self._mtime()
        if stamp != self._stamp:
            out: dict[str, Agent] = {}
            for path in self._files():
                try:
                    agent = agent_from_markdown(
                        path.read_text(encoding="utf-8"), path.stem, f"file:{path}")
                except Exception:
                    logger.exception("could not load agent %s", path)
                    continue
                if not agent.prompt:
                    logger.warning("agent %s has an empty prompt body — skipping", path.name)
                    continue
                if agent.name in out:
                    logger.warning("agent %r is defined twice; keeping %s, ignoring %s",
                                   agent.name, out[agent.name].source, path)
                    continue
                out[agent.name] = agent
            self._cache, self._stamp = out, stamp
            logger.info("loaded %d agent(s) from %s: %s", len(out),
                        ", ".join(str(d) for d in self.dirs), ", ".join(sorted(out)) or "none")
        return self._cache


_store: AgentStore | None = None


def store() -> AgentStore:
    """FileStore when agents/ has definitions, else the env-var layout."""
    global _store
    if _store is None:
        fs = FileStore(AGENTS_DIRS)
        _store = fs if fs.all() else EnvStore()
    return _store


def reset() -> None:
    """Drop the cached store — used by tests and after `proteus agent new`."""
    global _store
    _store = None
