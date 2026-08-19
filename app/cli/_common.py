"""Shared plumbing for the CLI: config loading, output, errors, HTTP."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

REPO = Path(__file__).resolve().parent.parent.parent

# stderr for diagnostics, stdout for data — so `proteus agent list --json | jq`
# works and warnings don't corrupt the pipe.
out = Console()
err = Console(stderr=True)

_env_loaded = False


def load_env() -> None:
    """Load .env with python-dotenv — the SAME loader app/config.py uses.

    Hand-rolling this is a trap: `TOOLS_SHELL=true  # locked down` parses to the
    string "true  # locked down" under a naive split, which is truthy-looking
    but fails every `== "true"` check, so the CLI would report a different
    toolset than the server actually serves.
    """
    global _env_loaded
    if _env_loaded:
        return
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env")
    _env_loaded = True


def die(message: str, hint: str = "") -> NoReturn:
    err.print(f"[red]error:[/] {message}")
    if hint:
        err.print(f"[dim]{hint}[/]")
    raise typer.Exit(1)


def emit(data: Any, as_json: bool, render) -> None:
    """One data path, two renderings. `render` draws the human version.

    --json writes RAW json to stdout with plain print, not through rich:
    rich colourises and re-wraps, which is lovely on a terminal and unparseable
    the moment you pipe it into jq.
    """
    if as_json:
        print(json.dumps(data, default=str, indent=2))
    else:
        render(data)


def table(*columns: str) -> Table:
    t = Table(*columns, box=None, pad_edge=False, header_style="bold")
    return t


def base_url(remote: str | None) -> str:
    load_env()
    return (remote or f"http://127.0.0.1:{os.environ.get('PORT', '18791')}").rstrip("/")


def api_key() -> str:
    load_env()
    return os.environ.get("API_KEY", "")


def try_json(url: str, timeout: float = 4.0) -> dict | None:
    """GET returning parsed JSON, or None. For probes where absence is a
    finding rather than a failure — `doctor` must still report everything else
    when the gateway happens to be down."""
    import httpx

    try:
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def get_json(url: str, timeout: float = 15.0) -> dict:
    """GET returning parsed JSON, with errors a human can act on."""
    import httpx

    try:
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except httpx.ConnectError:
        die(f"cannot reach {url}", "Is the gateway running?  proteus serve")
    except httpx.HTTPStatusError as exc:
        die(f"{url} returned HTTP {exc.response.status_code}",
            exc.response.text[:200] if exc.response.text else "")
    except httpx.HTTPError as exc:
        die(f"request to {url} failed: {type(exc).__name__}", str(exc)[:200])
    except json.JSONDecodeError:
        die(f"{url} did not return JSON", "Is that really a proteus gateway?")


def version() -> str:
    try:
        from importlib.metadata import version as _v

        return _v("proteus-gateway")
    except Exception:
        return "0.0.0+local"


def check_name(name: str, what: str) -> str:
    """Names become filenames, so refuse anything that could escape the directory."""
    clean = (name or "").strip()
    if not clean:
        die(f"{what} name is required")
    if clean != Path(clean).name or clean.startswith("."):
        die(f"invalid {what} name: {name!r}",
            "Use a plain name with no path separators, e.g. 'support'.")
    return clean


def confirm_tty(prompt: str) -> bool:
    """Confirm, but never block a script: no TTY means no implicit yes."""
    if not sys.stdin.isatty():
        die("this needs confirmation but stdin is not a terminal", "Pass --yes to proceed.")
    return typer.confirm(prompt)
