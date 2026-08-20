"""
Tools defined as files, not code.

Two kinds, both auto-discovered:

  tools/<name>.md        an HTTP call, no Python at all
  app/tools/custom/*.py  a Python handler, for anything with real logic

The Markdown form uses the same convention as agents — frontmatter for config,
body for prose — because a tool's `description` is prose the MODEL reads, and
how well it is written decides whether the tool ever gets called:

    ---
    name: weather
    method: GET
    url: https://api.example.com/weather
    auth: bearer ${WEATHER_KEY}
    query:
      city: {{city}}
    params:
      city: {type: string, required: true, description: City name}
    ---

    Current weather for a city. Use when the user asks about conditions today.

GROUPING. `toolset: <name>` puts a tool in a named toolset that agents ask for
by name, instead of the default "custom" bucket that every agent using custom
tools receives. That is what keeps one deployment's several products apart: a
general assistant has no business carrying another agent's domain tools.

SECURITY. Declarative tools get exactly the same treatment as built-ins:

  * `user_id` is injected by the gateway and can never be declared as a param,
    so a model cannot aim a tool at another user's data.
  * Substitution happens ONLY in the path, query and body — never in the scheme
    or host. Otherwise `url: https://{{host}}/` would hand the model an SSRF
    primitive against your internal network.
  * They are never host tools, so no declarative file can gain shell-equivalent
    access (see toolsets.HOST_TOOLS).
  * `${VAR}` in auth/headers reads from the environment at call time, so secrets
    live in the environment rather than in a file someone might commit.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .. import config
from ..agents_store import parse_markdown
from ..httpclient import get_client

logger = logging.getLogger("proteus.tools.declarative")

_APP_DIR = Path(__file__).parent.parent
_REPO_DIR = _APP_DIR.parent
# The first of each is this deployment's own and where `proteus tool new`
# writes; the rest come from mounted packs. See config.PACK_DIRS.
TOOLS_DIR = config.TOOLS_DIR
TOOLS_DIRS = config.TOOLS_DIRS
CUSTOM_DIR = config.CUSTOM_TOOLS_DIR
CUSTOM_DIRS = config.CUSTOM_TOOLS_DIRS

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_ENVVAR = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")

# Never accept these as declared parameters: the gateway owns identity, and a
# tool that could take a user id would let the model pick whose data to touch.
RESERVED_PARAMS = frozenset({"user_id", "userid", "user"})

# A file-defined tool belongs to the toolset named in its `toolset:` frontmatter
# (Python tools: a module-level TOOLSET). Untagged files land in "custom", which
# is what every existing tool file means by saying nothing. Tagging is how one
# deployment can define tools for several agents without every agent seeing all
# of them — a general assistant should not be carrying another product's tools.
DEFAULT_GROUP = "custom"

# `_fill` returns this for a key whose only content was an argument the caller
# never supplied, so the key is dropped rather than sent empty.
_OMIT = object()


def _env_expand(value: str) -> str:
    return _ENVVAR.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _fill(template: Any, args: dict[str, Any]) -> Any:
    """Substitute {{name}} from args. Recurses through dicts and lists."""
    if isinstance(template, str):
        lone = _PLACEHOLDER.fullmatch(template.strip())
        if lone:
            # A value that is EXACTLY one placeholder passes the argument
            # through with its type intact, so `count: "{{count}}"` sends the
            # number 5 rather than the string "5". An argument the model didn't
            # supply drops its key instead: sending "" would overwrite the
            # backend's own default with an empty value, which is how an
            # omitted `limit` silently became a limit of nothing.
            value = args.get(lone.group(1), _OMIT)
            return _OMIT if value is None else value

        def sub(m):
            v = args.get(m.group(1))
            return "" if v is None else str(v)
        return _PLACEHOLDER.sub(sub, template)
    if isinstance(template, dict):
        return {k: v for k, v in ((k, _fill(v, args)) for k, v in template.items())
                if v is not _OMIT}
    if isinstance(template, list):
        return [v for v in (_fill(item, args) for item in template) if v is not _OMIT]
    return template


def _schema_from(meta: dict[str, Any], description: str, name: str) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, spec in (meta.get("params") or {}).items():
        if pname.lower() in RESERVED_PARAMS:
            logger.warning("tool %s: ignoring reserved parameter %r", name, pname)
            continue
        spec = spec if isinstance(spec, dict) else {"type": "string"}
        prop = {"type": spec.get("type", "string")}
        if spec.get("description"):
            prop["description"] = str(spec["description"])
        if spec.get("enum"):
            prop["enum"] = list(spec["enum"])
        props[pname] = prop
        if spec.get("required"):
            required.append(pname)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description.strip(),
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


class HttpTool:
    def __init__(self, name: str, meta: dict[str, Any], description: str, source: str) -> None:
        self.name = name
        self.meta = meta
        self.source = source
        self.schema = _schema_from(meta, description, name)
        self.group = str(meta.get("toolset") or DEFAULT_GROUP).strip() or DEFAULT_GROUP
        # `${VAR}` is expanded HERE, at load, not per call: the base URL of a
        # backend is deployment config, so it belongs in the environment rather
        # than committed in the file — and expanding it now means the host check
        # below sees the address the request will really go to. Unlike {{args}},
        # this substitution is the operator's, never the model's.
        self.url = _env_expand(str(meta.get("url") or ""))
        self.method = str(meta.get("method") or "GET").upper()
        # Per-tool override, because the shared client's short timeout is right
        # for fetching a web page and wrong for a backend that does real work
        # (a cold headless-browser scrape, an LLM-backed endpoint).
        self.timeout = float(meta["timeout"]) if meta.get("timeout") else None
        if not self.url:
            raise ValueError(f"tool {name}: 'url' is required")
        if "://" not in self.url:
            # Almost always an unset ${VAR} in the url. Failing at load names
            # the problem once; failing per call would surface as a confusing
            # connection error inside a conversation.
            raise ValueError(
                f"tool {name}: url {self.url!r} has no scheme — is an environment "
                f"variable in it unset?")
        # Substitution must not be able to change WHERE the request goes.
        host_part = self.url.split("//", 1)[-1].split("/", 1)[0]
        if _PLACEHOLDER.search(host_part):
            raise ValueError(
                f"tool {name}: placeholders are not allowed in the scheme or host "
                f"({host_part!r}) — that would let the model redirect the request")

    async def __call__(self, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
        args = {k: v for k, v in (args or {}).items() if k.lower() not in RESERVED_PARAMS}
        headers = {k: _env_expand(str(v)) for k, v in (self.meta.get("headers") or {}).items()}
        auth = self.meta.get("auth")
        if auth:
            headers["Authorization"] = _env_expand(str(auth))
        if self.meta.get("send_user_header"):
            # Opt-in: pass the gateway-resolved identity to a trusted backend.
            headers[str(self.meta["send_user_header"])] = user_id

        url = _fill(self.url, args)
        params = _fill(self.meta.get("query") or {}, args) or None
        body = _fill(self.meta.get("body"), args) if self.meta.get("body") is not None else None
        try:
            r = await get_client().request(self.method, url, headers=headers or None,
                                           params=params, json=body, timeout=self.timeout
                                           if self.timeout else httpx.USE_CLIENT_DEFAULT)
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}", "body": r.text[:300]}
            ctype = r.headers.get("content-type", "")
            return r.json() if "json" in ctype else {"text": r.text[:8000]}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


def _load_http_tools() -> dict[str, HttpTool]:
    out: dict[str, HttpTool] = {}
    for directory in TOOLS_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                meta, body = parse_markdown(path.read_text(encoding="utf-8"), name_hint=path.stem)
                name = str(meta.get("name") or path.stem).strip()
                if name in out:
                    logger.warning("tool %r is defined twice; keeping %s, ignoring %s",
                                   name, out[name].source, path)
                    continue
                out[name] = HttpTool(name, meta,
                                     body or str(meta.get("description") or name), str(path))
            except Exception as exc:
                logger.warning("skipping tool %s: %s", path, exc)
    return out


def _import_module(path: Path, index: int):
    """Import one custom-tool file.

    The module name carries the directory's index because two packs may each
    ship a `memory.py`, and identical names would collide in `sys.modules` —
    the second would silently never load. The directory goes on `sys.path` for
    the duration so a pack's `_shared.py` helper is importable by its tools;
    that underscore prefix already means "helper, not a tool", but until now
    nothing could actually import one.
    """
    directory = str(path.parent)
    added = directory not in sys.path
    if added:
        sys.path.insert(0, directory)
    try:
        spec = importlib.util.spec_from_file_location(
            f"proteus_custom_{index}_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if added:
            sys.path.remove(directory)


class PyTool:
    def __init__(self, schema: dict, handler: Callable[..., Awaitable[dict]],
                 group: str, source: str) -> None:
        self.schema = schema
        self.handler = handler
        self.group = group
        self.source = source
        self.name = schema.get("function", {}).get("name", "")

    async def __call__(self, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return await self.handler(user_id, args)


def _load_python_tools() -> dict[str, PyTool]:
    """app/tools/custom/*.py exporting SCHEMA + handler.

    A module may also export `TOOLS = [(schema, handler), …]` when several tools
    share one implementation — three ways to touch the same store is one file,
    not three. `TOOLSET = "name"` puts the module's tools in a named toolset;
    files starting with `_` are helpers and are never loaded as tools.
    """
    out: dict[str, PyTool] = {}
    for index, directory in enumerate(CUSTOM_DIRS):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                mod = _import_module(path, index)
                group = str(getattr(mod, "TOOLSET", "") or DEFAULT_GROUP).strip() or DEFAULT_GROUP

                pairs = list(getattr(mod, "TOOLS", None) or [])
                schema, handler = getattr(mod, "SCHEMA", None), getattr(mod, "handler", None)
                if schema and handler:
                    pairs.append((schema, handler))
                if not pairs:
                    logger.warning("custom tool %s must export SCHEMA and handler (or TOOLS)",
                                   path)
                    continue

                for schema, handler in pairs:
                    name = schema.get("function", {}).get("name") or path.stem
                    if name in out:
                        logger.warning("tool %r is defined twice; keeping %s, ignoring %s",
                                       name, out[name].source, path)
                        continue
                    out[name] = PyTool(schema, handler, group, str(path))
            except Exception:
                logger.exception("could not load custom tool %s", path)
    return out


def load(group: str | None = None) -> tuple[list[dict], Callable[[str, str, dict], Awaitable[dict]]] | None:
    """(schemas, dispatch) for file-defined tools, or None if there are none.

    `group` selects one toolset's worth of them; None means every tool
    regardless of tag, which is what a caller wanting the whole set asks for.
    """
    tools: dict[str, HttpTool | PyTool] = {**_load_http_tools(), **_load_python_tools()}
    if group is not None:
        tools = {n: t for n, t in tools.items() if t.group == group}
    if not tools:
        return None

    async def dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = tools.get(name)
        if tool is None:
            return {"error": f"unknown tool: {name}"}
        return await tool(user_id, args)

    logger.info("file-defined tools (%s): %s", group or "all", ", ".join(sorted(tools)))
    return [t.schema for t in tools.values()], dispatch


def groups() -> set[str]:
    """Every toolset name claimed by a tool file."""
    return {t.group for t in _load_http_tools().values()} | \
           {t.group for t in _load_python_tools().values()}


def describe() -> list[dict[str, Any]]:
    """For `proteus tool list`."""
    rows = []
    for t in _load_http_tools().values():
        rows.append({"name": t.name, "kind": "http", "source": t.source, "toolset": t.group,
                     "target": f"{t.method} {t.url}",
                     "description": t.schema["function"]["description"][:80]})
    for t in _load_python_tools().values():
        rows.append({"name": t.name, "kind": "python", "source": t.source, "toolset": t.group,
                     "target": "-", "description": t.schema["function"].get("description", "")[:80]})
    return rows
