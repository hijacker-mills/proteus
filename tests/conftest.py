"""
Shared fixtures. The point of this file is that the unit suite must run with
NOTHING available: no database, no gateway, no provider key, no network.

Anything needing those is a live-integration script (the other files in this
directory), run by hand. Only what is here runs in CI, which is why it has to
stay honest about its dependencies.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Set before app.config is imported anywhere: it reads the environment at import
# time, so a fixture would be too late.
os.environ.setdefault("MODEL", "mock/instant")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("TOOLSET", "none")
os.environ.setdefault("AGENTS_DIR", "")


@pytest.fixture
def tmp_agents(tmp_path, monkeypatch):
    """An isolated agents/ directory, so tests never see the real one."""
    from app import agents_store

    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(agents_store, "AGENTS_DIR", directory)
    agents_store.reset()
    yield directory
    agents_store.reset()


@pytest.fixture
def tmp_tools(tmp_path, monkeypatch):
    """An isolated tools/ directory for declarative tools."""
    from app.tools import declarative

    directory = tmp_path / "tools"
    directory.mkdir()
    monkeypatch.setattr(declarative, "TOOLS_DIR", directory)
    yield directory


def write_agent(directory: Path, name: str, frontmatter: str, body: str = "You are a test agent.") -> Path:
    path = directory / f"{name}.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return path
