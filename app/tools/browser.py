"""
Browser tool — drives a real headless Chrome by shelling out to `pw-control`
(the user's Playwright/CDP control CLI), via its agent-friendly `--ai` JSON output.

One persistent headless Chrome on CDP :PORT is launched lazily and reused across
calls (and across acag processes — they share the same CDP endpoint). The agent
gets a single `browser` tool with actions: navigate / read / click / click_text /
fill / type / press / eval / back. Interaction actions auto-return a fresh page
snapshot (title, text, links, buttons, inputs) so the model "sees" the result.

Reading uses pw-control `snapshot` (and `eval-js`) rather than screenshots —
headless Chrome here uses software GL and screenshots are unreliable.
"""
from __future__ import annotations

import asyncio
import json
import os

from .. import config

_BIN = config.PW_CONTROL_BIN
_PORT = str(config.PW_CONTROL_CDP_PORT)
_ENV = {**os.environ, "PATH": os.path.dirname(_BIN) + os.pathsep + os.environ.get("PATH", "")}
_launch_lock = asyncio.Lock()


async def _port_open() -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", int(_PORT)), timeout=2)
        w.close()
        return True
    except Exception:
        return False


async def _pw(args: list[str]) -> dict:
    cmd = [_BIN, *args, "--ai", "--cdp-port", _PORT]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_ENV
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=config.TOOLS_BROWSER_TIMEOUT)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "browser action timed out"}
    except Exception as exc:
        return {"ok": False, "error": f"pw-control spawn failed: {exc}"}
    raw = out.decode("utf-8", "replace").strip()
    a, b = raw.find("{"), raw.rfind("}")
    if a == -1:
        return {"ok": False, "error": (err.decode("utf-8", "replace")[:200] or "no output")}
    try:
        return json.loads(raw[a:b + 1])
    except Exception:
        return {"ok": False, "error": "unparseable pw-control output"}


async def _ensure_chrome() -> bool:
    if await _port_open():
        return True
    async with _launch_lock:
        if await _port_open():
            return True
        await _pw(["navigate", "about:blank", "--launch-chrome", "--headless", "--no-sandbox"])
        for _ in range(12):
            if await _port_open():
                return True
            await asyncio.sleep(0.5)
        return await _port_open()


async def _snapshot(max_text: int = 1500) -> dict:
    r = await _pw(["snapshot", "--max-text", str(max_text)])
    d = r.get("data", {}) if isinstance(r, dict) else {}
    return {
        "title": d.get("title"),
        "url": d.get("url"),
        "text": d.get("text"),
        "links": (d.get("links") or [])[:15],
        "buttons": (d.get("buttons") or [])[:12],
        "inputs": (d.get("inputs") or [])[:12],
    }


async def browser(
    action: str,
    url: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    key: str | None = None,
    code: str | None = None,
) -> dict:
    if not await _ensure_chrome():
        return {"error": "could not start browser (Chrome failed to launch)"}

    action = (action or "").strip()
    try:
        if action == "navigate":
            if not url:
                return {"error": "url required for navigate"}
            r = await _pw(["navigate", url])
            if not r.get("ok"):
                return {"error": r.get("error", "navigate failed")}
            return {"ok": True, "action": "navigate", **await _snapshot()}

        if action in ("read", "snapshot"):
            return {"ok": True, "action": "read", **await _snapshot(3000)}

        if action == "click":
            if not selector:
                return {"error": "selector required for click"}
            r = await _pw(["click", selector])
            return {"ok": r.get("ok", False), "action": "click", "error": r.get("error"), **await _snapshot()}

        if action == "click_text":
            r = await _pw(["click-text", text or ""])
            return {"ok": r.get("ok", False), "action": "click_text", "error": r.get("error"), **await _snapshot()}

        if action == "fill":
            if not selector:
                return {"error": "selector required for fill"}
            r = await _pw(["fill", selector, text or ""])
            return {"ok": r.get("ok", False), "action": "fill", "error": r.get("error"), **await _snapshot()}

        if action == "type":
            if not selector:
                return {"error": "selector required for type"}
            r = await _pw(["type", selector, text or ""])
            return {"ok": r.get("ok", False), "action": "type", "error": r.get("error"), **await _snapshot()}

        if action == "press":
            r = await _pw(["press", key or "Enter"])
            return {"ok": r.get("ok", False), "action": "press", "error": r.get("error"), **await _snapshot()}

        if action == "back":
            await _pw(["eval-js", "history.back()"])
            await asyncio.sleep(0.6)
            return {"ok": True, "action": "back", **await _snapshot()}

        if action == "eval":
            r = await _pw(["eval-js", code or ""])
            return {"ok": r.get("ok", False), "action": "eval",
                    "result": (r.get("data") or {}).get("result"), "error": r.get("error")}

        return {"error": f"unknown browser action: {action}"}
    except Exception as exc:
        return {"error": f"browser failed: {exc}"}
