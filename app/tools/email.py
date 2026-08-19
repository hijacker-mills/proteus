"""
email — wraps the himalaya CLI (its configured IMAP/SMTP accounts).

Actions: list / search / read / send. Uses himalaya's default account unless one
is given (e.g. "gmail"). Read-only actions are list/search/read; send dispatches
a real email, so the agent should confirm intent first.
"""
from __future__ import annotations

import asyncio
import json
import os

from .. import config

_ENV = {**os.environ,
        "PATH": ":".join(p for p in (config.TOOLS_EXTRA_PATH, os.environ.get("PATH", "")) if p)}


async def _run(args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        config.HIMALAYA_BIN, *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_ENV,
    )
    out, err = await asyncio.wait_for(
        proc.communicate(stdin.encode() if stdin is not None else None),
        timeout=config.TOOLS_EMAIL_TIMEOUT,
    )
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _from_field(env: dict) -> str | None:
    f = env.get("from")
    if isinstance(f, dict):
        return f.get("addr") or f.get("name")
    return f if isinstance(f, str) else None


async def email(action: str, to: str | None = None, subject: str | None = None,
                body: str | None = None, id: str | None = None, query: str | None = None,
                folder: str = "INBOX", account: str | None = None, limit: int = 10) -> dict:
    if not config.TOOLS_EMAIL:
        return {"error": "email is disabled (set TOOLS_EMAIL=true)"}
    acct = ["-a", account] if account else []
    try:
        if action in ("list", "search"):
            args = ["envelope", "list", "-o", "json", *acct, "-f", folder]
            if action == "search" and query:
                args.append(query)
            code, out, err = await _run(args)
            if code != 0:
                return {"error": (err or out)[:300] or "list failed"}
            data = json.loads(out)
            msgs = [{
                "id": e.get("id"),
                "subject": e.get("subject"),
                "from": _from_field(e),
                "date": e.get("date"),
                "flags": e.get("flags"),
            } for e in (data or [])[:limit]]
            return {"account": account or "default", "folder": folder, "messages": msgs}

        if action == "read":
            if not id:
                return {"error": "id required for read"}
            code, out, err = await _run(["message", "read", str(id), *acct])
            if code != 0:
                return {"error": (err or out)[:300] or "read failed"}
            return {"id": id, "content": out[:6000]}

        if action == "send":
            if not to or not body:
                return {"error": "to and body required for send"}
            raw = f"To: {to}\nSubject: {subject or '(no subject)'}\n\n{body}\n"
            code, out, err = await _run(["message", "send", *acct], stdin=raw)
            if code != 0:
                return {"error": (err or out)[:300] or "send failed"}
            return {"ok": True, "sent_to": to, "subject": subject or "(no subject)"}

        return {"error": f"unknown email action: {action}"}
    except asyncio.TimeoutError:
        return {"error": "email command timed out"}
    except Exception as exc:
        return {"error": str(exc)}
