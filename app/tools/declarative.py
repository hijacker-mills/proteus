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
from pathlib import Path
from typing import Any, Awaitable, Callable

from .. import config
from ..agents_store import parse_markdown
from ..httpclient import get_client

logger = logging.getLogger("proteus.tools.declarative")

_APP_DIR = Path(__file__).parent.parent
_REPO_DIR = _APP_DIR.parent
TOOLS_DIR = Path(config.TOOLS_DIR) if config.TOOLS_DIR else (_REPO_DIR / "tools")
CUSTOM_DIR = _APP_DIR / "tools" / "custom"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_ENVVAR = re.compile(r"\$\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")

# Never accept these as declared parameters: the gateway owns identity, and a
# tool that could take a user id would let the model pick whose data to touch.
RESERVED_PARAMS = frozenset({"user_id", "userid", "user"})


def _env_expand(value: str) -> str:
    return _ENVVAR.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _fill(template: Any, args: dict[str, Any]) -> Any:
    """Substitute {{name}} from args. Recurses through dicts and lists."""
    if isinstance(template, str):
        def sub(m):
            v = args.get(m.group(1))
            return "" if v is None else str(v)
        return _PLACEHOLDER.sub(sub, template)
    if isinstance(template, dict):
        return {k: _fill(v, args) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, args) for v in template]
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
        self.url = str(meta.get("url") or "")
        self.method = str(meta.get("method") or "GET").upper()
        if not self.url:
            raise ValueError(f"tool {name}: 'url' is required")
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
                                           params=params, json=body)
            if r.status_code >= 400:
                return {"error": f"HTTP {r.status_code}", "body": r.text[:300]}
            ctype = r.headers.get("content-type", "")
            return r.json() if "json" in ctype else {"text": r.text[:8000]}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


def _load_http_tools() -> dict[str, HttpTool]:
    out: dict[str, HttpTool] = {}
    if not TOOLS_DIR.is_dir():
        return out
    for path in sorted(TOOLS_DIR.glob("*.md")):
        try:
            meta, body = parse_markdown(path.read_text(encoding="utf-8"), name_hint=path.stem)
            name = str(meta.get("name") or path.stem).strip()
            out[name] = HttpTool(name, meta, body or str(meta.get("description") or name), path.name)
        except Exception as exc:
            logger.warning("skipping tool %s: %s", path.name, exc)
    return out


def _load_python_tools() -> dict[str, tuple[dict, Callable[..., Awaitable[dict]]]]:
    """app/tools/custom/*.py exporting SCHEMA (OpenAI function schema) and handler."""
    out: dict[str, tuple[dict, Callable]] = {}
    if not CUSTOM_DIR.is_dir():
        return out
    for path in sorted(CUSTOM_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"proteus_custom_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            schema, handler = getattr(mod, "SCHEMA", None), getattr(mod, "handler", None)
            if not schema or not handler:
                logger.warning("custom tool %s must export SCHEMA and handler", path.name)
                continue
            name = schema.get("function", {}).get("name") or path.stem
            out[name] = (schema, handler)
        except Exception:
            logger.exception("could not load custom tool %s", path.name)
    return out


def load() -> tuple[list[dict], Callable[[str, str, dict], Awaitable[dict]]] | None:
    """(schemas, dispatch) for every file-defined tool, or None if there are none."""
    http_tools = _load_http_tools()
    py_tools = _load_python_tools()
    if not http_tools and not py_tools:
        return None

    schemas = [t.schema for t in http_tools.values()] + [s for s, _ in py_tools.values()]

    async def dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in http_tools:
            return await http_tools[name](user_id, args)
        if name in py_tools:
            return await py_tools[name][1](user_id, args)
        return {"error": f"unknown tool: {name}"}

    logger.info("file-defined tools: %d http, %d python", len(http_tools), len(py_tools))
    return schemas, dispatch


def describe() -> list[dict[str, Any]]:
    """For `proteus tool list`."""
    rows = []
    for t in _load_http_tools().values():
        rows.append({"name": t.name, "kind": "http", "source": t.source,
                     "target": f"{t.method} {t.url}",
                     "description": t.schema["function"]["description"][:80]})
    for name, (schema, _) in _load_python_tools().items():
        rows.append({"name": name, "kind": "python", "source": f"custom/{name}.py",
                     "target": "-", "description": schema["function"].get("description", "")[:80]})
    return rows
