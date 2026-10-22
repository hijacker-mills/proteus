"""
Text from separate tool rounds must not run together.

On 2026-10-21 Qubi wrote "I'll use that one." before a tool call and "You have
5 Data Structures cards due." after it, and the student saw "I'll use that
one.You have 5 ...". Each round is its own model call, so the gateway owns the
gap between them.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import write_agent

TOOL = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}


def _scripted_model(rounds, calls):
    """Round i streams rounds[i] as text chunks, and calls `lookup` unless it is the last."""
    async def astream(*, model, messages, tools, tool_choice="auto", **_):
        calls.append(list(messages))
        i = len(calls) - 1
        for chunk in rounds[i]:
            yield {"type": "text", "text": chunk}
        if i < len(rounds) - 1:
            yield {"type": "final", "finish_reason": "tool_calls", "usage": {},
                   "tool_calls": [{"id": f"c{i}", "name": "lookup", "arguments": {}}]}
        else:
            yield {"type": "final", "tool_calls": [], "finish_reason": "stop", "usage": {}}
    return astream


def _reply(monkeypatch, tmp_agents, rounds):
    from app import agent, llm, profiles

    write_agent(tmp_agents, "tutor", "name: tutor\ntool_rounds: 5")

    async def dispatch(name, user_id, args):
        return {"ok": True}

    calls = []
    monkeypatch.setattr(profiles, "resolve", lambda name, host_tools=False: ("sys", [TOOL], dispatch))
    monkeypatch.setattr(llm, "astream", _scripted_model(rounds, calls))

    async def collect():
        return [ev async for ev in agent.run("u1", [{"role": "user", "content": "my cards"}],
                                             profile="tutor")]
    events = asyncio.run(collect())
    return "".join(ev["text"] for ev in events if ev["type"] == "text"), calls


def test_rounds_get_a_paragraph_break(monkeypatch, tmp_agents):
    text, _ = _reply(monkeypatch, tmp_agents, [
        ["I'll use ", "that one."],
        ["You have 5 ", "cards due."],
    ])
    assert text == "I'll use that one.\n\nYou have 5 cards due."


def test_every_round_boundary_gets_one(monkeypatch, tmp_agents):
    text, _ = _reply(monkeypatch, tmp_agents, [["Checking."], ["Still checking."], ["Done."]])
    assert text == "Checking.\n\nStill checking.\n\nDone."


@pytest.mark.parametrize("rounds, expected", [
    ([["Checking.\n"], ["Done."]], "Checking.\nDone."),     # model already ended the line
    ([["Checking."], [" Done."]], "Checking. Done."),       # model already opened with a space
    ([[], ["Done."]], "Done."),                             # nothing before the tool call
    ([["Checking."], ["", "Done."]], "Checking.\n\nDone."),  # an empty first chunk is skipped
])
def test_no_break_where_there_is_already_a_gap(monkeypatch, tmp_agents, rounds, expected):
    text, _ = _reply(monkeypatch, tmp_agents, rounds)
    assert text == expected


def test_the_model_sees_its_own_text_unchanged(monkeypatch, tmp_agents):
    _, calls = _reply(monkeypatch, tmp_agents, [["I'll use that one."], ["You have 5 cards due."]])
    tool_turn = next(m for m in calls[1] if m["role"] == "assistant")
    assert tool_turn["content"] == "I'll use that one."
