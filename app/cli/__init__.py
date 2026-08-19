"""The `proteus` command line.

Proteus serves many agents from one gateway, and this manages them: which
agents exist, what each is made of, and which tools it can reach. Adding an
agent is adding a file, not deploying another process. Everything it writes is a file
in the repo, so a change is reviewable and revertible like any other.

Most commands work on local files. Those that inspect a running gateway
(`health`, `chat`, `bench`) accept `--remote URL` so the same CLI drives the
box in production.
"""
from __future__ import annotations

from .main import app, run

__all__ = ["app", "run"]
