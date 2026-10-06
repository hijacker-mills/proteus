"""
The tool budget: a reply that runs out of tool rounds or tool time still
answers, from what it already looked up.

On 2026-10-06 a student asked Qubi for every bone in the body. It searched
eight times, hit the round limit, and the run ended on an apology after 153 s,
discarding all eight results. These tests pin the replacement behaviour.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

from conftest import write_agent

TOOL = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}


def _fake_model(calls, *, ignore_tool_choice=False, answer="Here is the answer."):
    """A model that calls `lookup` whenever it is allowed to, and answers otherwise."""
    async def astream(*, model, messages, tools, tool_choice="auto", **_):
        calls.append({"tool_choice": tool_choice, "messages": list(messages)})
        if tool_choice == "none" and not ignore_tool_choice:
            yield {"type": "text", "text": answer}
            yield {"type": "final", "tool_calls": [], "finish_reason": "stop", "usage": {}}
            return
        n = len(calls)
        yield {"type": "final", "finish_reason": "tool_calls", "usage": {},
               "tool_calls": [{"id": f"c{n}", "name": "lookup", "arguments": {"q": n}}]}
    return astream


def _run(monkeypatch, tmp_agents, frontmatter, calls, **model_kw):
    from app import agent, llm, profiles

    write_agent(tmp_agents, "tutor", "name: tutor\n" + frontmatter)
    dispatched = []

    async def dispatch(name, user_id, args):
        dispatched.append(args)
        return {"found": args}

    monkeypatch.setattr(profiles, "resolve", lambda name, host_tools=False: ("sys", [TOOL], dispatch))
    monkeypatch.setattr(llm, "astream", _fake_model(calls, **model_kw))

    async def collect():
        return [ev async for ev in agent.run("u1", [{"role": "user", "content": "all the bones"}],
                                             profile="tutor")]
    return asyncio.run(collect()), dispatched


def _text(events):
    return "".join(ev["text"] for ev in events if ev["type"] == "text")


def test_out_of_rounds_answers_from_what_it_found(monkeypatch, tmp_agents):
    calls = []
    events, dispatched = _run(monkeypatch, tmp_agents, "tool_rounds: 3", calls)

    assert len(dispatched) == 3                      # every allowed round ran
    assert len(calls) == 4                           # plus one call that must answer
    assert [c["tool_choice"] for c in calls] == ["auto", "auto", "auto", "none"]
    final_messages = calls[-1]["messages"]
    assert final_messages[-1]["role"] == "system"
    assert "Do not call any more tools" in final_messages[-1]["content"]
    # the three lookups are still in front of the model when it answers
    assert sum(1 for m in final_messages if m["role"] == "tool") == 3
    assert _text(events) == "Here is the answer."
    done = events[-1]
    assert done["type"] == "done" and done["tool_budget_spent"] is True


def test_time_budget_stops_tool_calls_early(monkeypatch, tmp_agents):
    from app import agent

    clock = iter([0.0, 5.0, 50.0, 50.0, 50.0])      # start, after round 1, after round 2
    fake_time = types.SimpleNamespace(monotonic=lambda: next(clock), time=time.time)
    monkeypatch.setattr(agent, "time", fake_time)
    calls = []
    events, dispatched = _run(monkeypatch, tmp_agents, "tool_rounds: 8\ntool_time_budget: 45", calls)

    assert len(dispatched) == 2
    assert [c["tool_choice"] for c in calls] == ["auto", "auto", "none"]
    assert _text(events) == "Here is the answer."


def test_a_provider_that_ignores_tool_choice_gets_no_more_tools(monkeypatch, tmp_agents):
    calls = []
    events, dispatched = _run(monkeypatch, tmp_agents, "tool_rounds: 2", calls,
                              ignore_tool_choice=True)

    assert len(dispatched) == 2                      # the forced call's tool request is dropped
    assert "narrower question" in _text(events)
    assert events[-1]["type"] == "done"


def test_a_reply_inside_its_budget_is_unchanged(monkeypatch, tmp_agents):
    from app import agent, llm, profiles

    write_agent(tmp_agents, "tutor", "name: tutor\ntool_rounds: 3")
    seen = []

    async def astream(*, tool_choice="auto", **_):
        seen.append(tool_choice)
        yield {"type": "text", "text": "hi"}
        yield {"type": "final", "tool_calls": [], "finish_reason": "stop", "usage": {}}

    async def dispatch(*_):                          # pragma: no cover - never called
        raise AssertionError

    monkeypatch.setattr(profiles, "resolve", lambda name, host_tools=False: ("sys", [TOOL], dispatch))
    monkeypatch.setattr(llm, "astream", astream)

    async def collect():
        return [ev async for ev in agent.run("u1", [{"role": "user", "content": "hi"}], profile="tutor")]
    events = asyncio.run(collect())

    assert seen == ["auto"]
    assert _text(events) == "hi"
    assert events[-1]["tool_budget_spent"] is False


@pytest.mark.parametrize("frontmatter, rounds, seconds", [
    ("tool_rounds: 4\ntool_time_budget: 45", 4, 45),
    ("tool_rounds: zero\ntool_time_budget: -3", None, None),   # bad values are ignored
    ("description: none set", None, None),
])
def test_budget_frontmatter(tmp_agents, frontmatter, rounds, seconds):
    from app import agents_store, config, profiles

    write_agent(tmp_agents, "b", "name: b\n" + frontmatter)
    agent = agents_store.store().all()["b"]
    assert (agent.tool_rounds, agent.tool_time_budget) == (rounds, seconds)
    assert profiles.tool_budget_for("b") == (rounds or config.MAX_TOOL_TURNS,
                                            seconds or config.TOOL_TIME_BUDGET)
