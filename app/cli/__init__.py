"""The `proteus` command line.

Proteus serves many agents from one gateway, and this manages them: which
agents exist, what each is made of, and which tools it can reach. Adding an
agent is adding a file, not deploying another process.

Commands that touch a live instance take `--remote URL`, so the same CLI drives
production. List and inspect commands take `--json`, so they compose with `jq`
rather than needing to be parsed out of a table.
"""
from __future__ import annotations

from .main import app, run

__all__ = ["app", "run"]
