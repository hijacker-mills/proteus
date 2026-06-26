"""
Named profiles = (persona prompt, toolset). A request selects one (header
X-Acag-Profile, or the profile arg); channels use the default. This is how one
standalone ACAG can be a general assistant on one path and Qubi (the IntelliQ
study companion) on another, without separate instances.

Add a profile by adding an entry to _PROFILES.
"""
from __future__ import annotations

from pathlib import Path

from . import config, toolsets

_DIR = Path(__file__).parent

# name -> (prompt file relative to app/, toolset string)
_PROFILES: dict[str, tuple[str, str]] = {
    "assistant": (config.SYSTEM_PROMPT_FILE, config.TOOLSET),
    "qubi": (config.QUBI_PROMPT_FILE, config.QUBI_TOOLSET),
}

_cache: dict[str, tuple[str, list | None, object]] = {}


def names() -> list[str]:
    return list(_PROFILES)


def resolve(name: str | None):
    """Return (system_prompt_text, tools, dispatch) for a profile name."""
    key = name if name in _PROFILES else config.DEFAULT_PROFILE
    if key not in _PROFILES:
        key = "assistant"
    if key not in _cache:
        prompt_file, toolset_str = _PROFILES[key]
        prompt = (_DIR / prompt_file).read_text(encoding="utf-8")
        tools, dispatch = toolsets.load_for(toolset_str)
        _cache[key] = (prompt, tools, dispatch)
    return _cache[key]
