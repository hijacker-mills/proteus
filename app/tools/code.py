"""
run_code — execute a short Python snippet and return its output.

SECURITY: this runs real code on the host. It is OFF unless TOOLS_CODE_EXEC=true,
and you should restrict who can reach the bot (e.g. TELEGRAM_ALLOWED_USERS) before
enabling it — otherwise anyone who messages the bot gets code execution. Runs in
an isolated interpreter (`python -I`) in a temp dir with a wall-clock timeout. It
is NOT a strong sandbox (no namespace/network isolation); use containers/seccomp
for untrusted multi-user exposure.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

from .. import config


async def run_code(code: str) -> dict:
    if not config.TOOLS_CODE_EXEC:
        return {"error": "code execution is disabled (set TOOLS_CODE_EXEC=true and restrict bot access first)"}
    code = code or ""
    if not code.strip():
        return {"error": "code required"}

    with tempfile.TemporaryDirectory(prefix="acag_code_") as tmp:
        path = os.path.join(tmp, "snippet.py")
        with open(path, "w") as f:
            f.write(code)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmp,
            )
        except Exception as exc:
            return {"error": f"spawn failed: {exc}"}
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=config.TOOLS_CODE_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return {"error": f"timed out after {config.TOOLS_CODE_TIMEOUT}s"}

    return {
        "exit_code": proc.returncode,
        "stdout": out.decode("utf-8", "replace")[:4000],
        "stderr": err.decode("utf-8", "replace")[:2000],
    }
