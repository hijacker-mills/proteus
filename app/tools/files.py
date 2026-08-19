"""
Reading files. Three modes, chosen by FILES_ROOT.

    unset            both tools refuse. The default.
    /srv/docs:/data  confined to those roots. Safe for any agent, because it
                     can reach nothing sensitive. NOT a host tool.
    /                the whole filesystem, for a personal assistant whose user
                     is the operator. This IS a host tool.

That last distinction is the important one. `read_file` unconfined is
`shell`-equivalent in practice — `.env` holds your provider keys,
`~/.ssh/id_rsa` is a private key, and `~/.hermes/auth.json` is an OAuth token,
and all three are just paths. So in unrestricted mode the tools join
HOST_TOOLS and are withheld from any caller not explicitly trusted, exactly
like `shell`. An agent on a public surface never gets them, whatever profile it
asks for.

CONFINEMENT (the middle mode). Every path is resolved to an absolute real path
and only then checked against the roots. Resolving first is the point:
`../../etc/passwd`, an absolute path, and a symlink pointing out of the root all
collapse to something plainly outside it, whereas a string-prefix test on the
raw input misses every one.

Text only, size-capped, and binaries are refused rather than fed to a model as
mojibake.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger("proteus.tools.files")

# Extensions that are text but that a naive "is it decodable" check would let
# through as something worth reading. Kept short; the decode check does the work.
_SKIP_SUFFIXES = frozenset({
    ".pyc", ".so", ".o", ".a", ".dylib", ".dll", ".exe", ".bin", ".dat",
    ".zip", ".gz", ".tar", ".bz2", ".xz", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf",
    ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg", ".woff", ".woff2", ".ttf",
})


def _roots() -> list[Path]:
    out = []
    for raw in config.FILES_ROOTS:
        try:
            out.append(Path(raw).expanduser().resolve(strict=True))
        except OSError:
            logger.warning("FILES_ROOT entry %r does not exist", raw)
    return out


def _resolve(raw: str) -> Path | None:
    """Absolute real path the caller is allowed to touch, or None.

    `resolve()` collapses `..` and follows symlinks FIRST, so the containment
    check sees where the path actually lands rather than what it looked like.
    That is what catches `../../etc/passwd`, an absolute path, and a symlink
    pointing out of the root — a string-prefix test on the raw input misses all
    three.
    """
    roots = _roots()
    if not roots:
        return None

    if config.FILES_UNRESTRICTED:
        # Whole-filesystem mode: `~` and relative paths still resolve, but there
        # is nothing to contain them to. Gated as a host tool instead.
        try:
            candidate = Path(raw).expanduser()
            return (candidate if candidate.is_absolute()
                    else (Path.cwd() / candidate)).resolve()
        except OSError:
            return None

    for root in roots:
        try:
            candidate = (root / raw.lstrip("/")).resolve()
        except OSError:
            continue
        if candidate == root or root in candidate.parents:
            return candidate
    return None


def _display(path: Path) -> str:
    """Show a path relative to its root when confined, absolute when not."""
    for root in _roots():
        if path == root or root in path.parents:
            return str(path) if config.FILES_UNRESTRICTED else str(path.relative_to(root))
    return str(path)


async def read_file(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    if not _roots():
        return {"error": "file reading is not configured on this deployment (FILES_ROOT is unset)"}

    raw = str(args.get("path") or "").strip()
    if not raw:
        return {"error": "path is required"}

    path = _resolve(raw)
    if path is None:
        return {"error": f"refused: {raw!r} resolves outside the permitted directories"}
    if not path.is_file():
        return {"error": f"no such file: {raw}"}
    if path.suffix.lower() in _SKIP_SUFFIXES:
        return {"error": f"{raw} looks like a binary file"}

    size = path.stat().st_size
    if size > config.FILES_MAX_BYTES:
        return {"error": f"{raw} is {size} bytes; the limit is {config.FILES_MAX_BYTES}"}

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"error": f"{raw} is not valid UTF-8 text"}
    except OSError as exc:
        return {"error": f"could not read {raw}: {exc.strerror or exc}"}

    lines = text.splitlines()
    offset = max(0, int(args.get("offset") or 0))
    limit = int(args.get("limit") or 0) or len(lines)
    window = lines[offset:offset + limit]
    return {
        "path": _display(path),
        "lines": len(lines),
        "shown": f"{offset + 1}-{offset + len(window)}" if window else "none",
        "content": "\n".join(window),
        "truncated": offset + len(window) < len(lines),
    }


async def list_files(user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    roots = _roots()
    if not roots:
        return {"error": "file reading is not configured on this deployment (FILES_ROOT is unset)"}

    raw = str(args.get("path") or "").strip()
    directory = _resolve(raw) if raw else roots[0]
    if directory is None:
        return {"error": f"refused: {raw!r} resolves outside the permitted directories"}
    if not directory.is_dir():
        return {"error": f"not a directory: {raw or '.'}"}

    pattern = str(args.get("pattern") or "*").strip() or "*"
    try:
        entries = sorted(directory.glob(pattern))[:500]
    except (OSError, ValueError) as exc:
        return {"error": f"could not list: {exc}"}

    out = []
    for e in entries:
        # A glob can still surface a symlink pointing out of the root.
        if _resolve(str(e)) is None:
            continue
        out.append({"path": _display(e),
                    "type": "dir" if e.is_dir() else "file",
                    "bytes": e.stat().st_size if e.is_file() else None})
    return {"root": str(directory), "count": len(out), "entries": out}


TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file. Use it whenever the user names a file "
                       "they want read; do not guess at its contents.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path to the file. Absolute, or "
                                                      "relative to the configured root."},
            "offset": {"type": "integer", "description": "First line to return (0-based)."},
            "limit": {"type": "integer", "description": "How many lines to return."},
        }, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List files in a directory. Use this to find a file before "
                       "reading it, when the user is vague about the path.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory; omit for the root."},
            "pattern": {"type": "string", "description": "Glob, e.g. '*.md' or '**/*.txt'."},
        }, "required": []}}},
]

_HANDLERS = {"read_file": read_file, "list_files": list_files}


async def dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}
    return await handler(user_id, args or {})
