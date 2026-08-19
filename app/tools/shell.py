"""
shell — run a command on the host (bash). This is the universal tool: it makes
every installed CLI available to the agent (himalaya, aws, hf, git, pw-control,
gog-cli once installed, …). It's essentially Hermes's terminal.

SECURITY: full host access. OFF unless TOOLS_SHELL=true, and the bot MUST stay
restricted (TELEGRAM_ALLOWED_USERS) or it's remote shell for anyone. Runs with a
PATH that includes ~/.local/bin and the nvm bin dir so user CLIs resolve.
"""
from __future__ import annotations

import asyncio
import os

from .. import config

_ENV = {**os.environ,
        "PATH": ":".join(p for p in (config.TOOLS_EXTRA_PATH, os.environ.get("PATH", "")) if p)}


async def shell(command: str) -> dict:
    if not config.TOOLS_SHELL:
        return {"error": "shell is disabled (set TOOLS_SHELL=true and keep the bot restricted first)"}
    command = (command or "").strip()
    if not command:
        return {"error": "command required"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-lc", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_ENV, cwd=_HOME,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=config.TOOLS_SHELL_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return {"error": f"timed out after {config.TOOLS_SHELL_TIMEOUT}s"}
    except Exception as exc:
        return {"error": str(exc)}
    return {
        "exit_code": proc.returncode,
        "stdout": out.decode("utf-8", "replace")[:5000],
        "stderr": err.decode("utf-8", "replace")[:2000],
    }
