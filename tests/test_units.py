"""
The fast suite: no database, no gateway, no provider, no network.

Everything here runs in CI on a clean checkout in seconds. The live-integration
scripts alongside it are the ones that need real infrastructure, and they are
run by hand. Keeping the split sharp is what makes CI meaningful: a suite that
needs Postgres is a suite that gets skipped.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import write_agent


# ── agent definitions ────────────────────────────────────────────────────────

def test_frontmatter_parses(tmp_agents):
    from app import agents_store

    write_agent(tmp_agents, "support",
                "name: support\ndescription: Helps\ntoolset: [web, custom]\nmax_tokens: 900",
                "You are support.")
    agent = agents_store.store().all()["support"]
    assert agent.toolset == "web,custom"
    assert agent.max_tokens == 900
    assert agent.prompt == "You are support."


def test_toolset_accepts_both_spellings(tmp_agents):
    from app import agents_store

    write_agent(tmp_agents, "a", "name: a\ntoolset: [web, custom]")
    write_agent(tmp_agents, "b", "name: b\ntoolset: web,custom")
    agents = agents_store.store().all()
    assert agents["a"].toolset == agents["b"].toolset == "web,custom"


def test_prompt_only_file_is_valid(tmp_agents):
    """A plain prompt with no frontmatter is a usable agent, not an error."""
    from app import agents_store

    (tmp_agents / "plain.md").write_text("Just a persona.", encoding="utf-8")
    agent = agents_store.store().all()["plain"]
    assert agent.prompt == "Just a persona."
    assert agent.toolset == "none"


def test_broken_frontmatter_does_not_kill_the_loader(tmp_agents):
    """One malformed file must not take out every other agent."""
    from app import agents_store

    (tmp_agents / "bad.md").write_text("---\n:::not yaml:::\n---\nbody", encoding="utf-8")
    write_agent(tmp_agents, "good", "name: good")
    assert "good" in agents_store.store().all()


def test_empty_prompt_is_skipped(tmp_agents):
    from app import agents_store

    write_agent(tmp_agents, "hollow", "name: hollow", body="")
    assert "hollow" not in agents_store.store().all()


def test_signed_identity_proof_is_required_and_verified(monkeypatch):
    from app import config, main
    import hashlib
    import hmac
    import time

    secret = "s" * 32
    monkeypatch.setattr(config, "PROTEUS_IDENTITY_SECRET", secret)
    user_id = "user-123"
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), f"{user_id}:{timestamp}".encode(), hashlib.sha256
    ).hexdigest()

    main._verify_identity(user_id, timestamp, signature)

    with pytest.raises(Exception, match="invalid user identity proof"):
        main._verify_identity(user_id, timestamp, "0" * 64)


def test_signed_identity_proof_rejects_expired_timestamp(monkeypatch):
    from app import config, main
    import hashlib
    import hmac
    import time

    secret = "s" * 32
    monkeypatch.setattr(config, "PROTEUS_IDENTITY_SECRET", secret)
    timestamp = str(int(time.time()) - 301)
    signature = hmac.new(
        secret.encode(), f"user-123:{timestamp}".encode(), hashlib.sha256
    ).hexdigest()

    with pytest.raises(Exception, match="expired user identity proof"):
        main._verify_identity("user-123", timestamp, signature)


def test_signed_identity_proof_rejects_missing_and_future_headers(monkeypatch):
    from app import config, main
    import time

    monkeypatch.setattr(config, "PROTEUS_IDENTITY_SECRET", "s" * 32)

    with pytest.raises(Exception, match="missing user identity proof"):
        main._verify_identity("user-123", None, None)

    with pytest.raises(Exception, match="future-dated user identity proof"):
        main._verify_identity("user-123", str(int(time.time()) + 61), "0" * 64)


def test_signed_identity_helper_round_trips_and_binds_user(monkeypatch):
    from app import config, identity, main

    monkeypatch.setattr(config, "PROTEUS_IDENTITY_SECRET", "s" * 32)
    headers = identity.signed_headers(" user-123 ")
    main._verify_identity(
        "user-123",
        headers["X-Proteus-Identity-Timestamp"],
        headers["X-Proteus-Identity-Signature"].upper(),
    )

    with pytest.raises(Exception, match="invalid user identity proof"):
        main._verify_identity(
            "user-456",
            headers["X-Proteus-Identity-Timestamp"],
            headers["X-Proteus-Identity-Signature"],
        )


def test_modes_come_from_the_agent(tmp_agents):
    from app import agents_store

    write_agent(tmp_agents, "m", "name: m\nmodes:\n  terse: Be brief.\n  long: Be thorough.")
    agent = agents_store.store().all()["m"]
    assert sorted(agent.modes) == ["long", "terse"]
    assert agent.mode_block("terse") == "Be brief."
    assert agent.mode_block("nope") == ""          # unknown mode injects nothing
    assert agent.mode_block(None) == ""


def test_hot_reload_on_change(tmp_agents):
    from app import agents_store

    write_agent(tmp_agents, "x", "name: x\ndescription: first")
    assert agents_store.store().all()["x"].description == "first"
    write_agent(tmp_agents, "x", "name: x\ndescription: second")
    assert agents_store.store().all()["x"].description == "second"


# ── SSRF guard ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # AWS/GCP metadata
    "http://metadata.google.internal/",
    "http://127.0.0.1/",
    "http://localhost/",
    "http://192.168.1.1/",
    "http://10.0.0.1/",
    "http://100.64.1.1/",                          # CGNAT: is_private says False
    "http://[::ffff:169.254.169.254]/",            # IPv4-mapped metadata
    "file:///etc/passwd",
    "ftp://example.com/",
    "http://no-such-host-xyz.invalid/",            # DNS failure -> fail closed
    "",
])
def test_unsafe_urls_are_blocked(url):
    from app.tools import url_safety

    assert url_safety.is_safe_url(url) is False


def test_metadata_stays_blocked_even_when_private_is_allowed(monkeypatch):
    """The always-blocked tier must not be reachable via configuration."""
    from app import config
    from app.tools import url_safety

    monkeypatch.setattr(config, "ALLOW_PRIVATE_URLS", True)
    assert url_safety.is_safe_url("http://169.254.169.254/") is False
    assert url_safety.is_safe_url("http://100.100.100.200/") is False   # Alibaba


# ── calculator ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expression,expected", [
    ("2+2", 4), ("(1234*5678)/3", 2335550.6666666665),
    ("10 % 3", 1), ("2**8", 256), ("-5 + 3", -2), ("7 // 2", 3),
])
def test_calculate(expression, expected):
    from app.tools.basics import calculate

    assert asyncio.run(calculate("u", {"expression": expression}))["result"] == expected


@pytest.mark.parametrize("expression", [
    "__import__('os').system('id')",      # the reason this is an AST walk, not eval
    "open('/etc/passwd').read()",
    "9**9**9",                            # would hang the worker
    "[].__class__.__mro__",
    "",
])
def test_calculate_refuses_anything_dangerous(expression):
    from app.tools.basics import calculate

    assert "error" in asyncio.run(calculate("u", {"expression": expression}))


def test_calculate_reports_division_by_zero(monkeypatch):
    from app.tools.basics import calculate

    assert asyncio.run(calculate("u", {"expression": "1/0"}))["error"] == "division by zero"


# ── datetime ─────────────────────────────────────────────────────────────────

def test_datetime_now_and_shift():
    from app.tools.basics import now

    utc = asyncio.run(now("u", {}))
    assert utc["timezone"] == "UTC" and len(utc["date"]) == 10
    week_ago = asyncio.run(now("u", {"shift_days": -7}))
    assert week_ago["date"] < utc["date"]
    assert "error" in asyncio.run(now("u", {"timezone": "Mars/Olympus"}))


# ── declarative tools ────────────────────────────────────────────────────────

def test_placeholder_in_host_is_rejected(tmp_tools):
    """A model argument must never be able to move the request's host."""
    from app.tools import declarative

    (tmp_tools / "evil.md").write_text(
        "---\nname: evil\nurl: https://{{host}}/x\nparams:\n  host: {type: string}\n---\nbad",
        encoding="utf-8")
    assert "evil" not in declarative._load_http_tools()


def test_placeholder_in_path_is_allowed(tmp_tools):
    from app.tools import declarative

    (tmp_tools / "ok.md").write_text(
        "---\nname: ok\nurl: https://api.example.com/{{slug}}\nparams:\n  slug: {type: string}\n---\nfine",
        encoding="utf-8")
    assert "ok" in declarative._load_http_tools()


def test_user_id_cannot_be_a_declared_parameter(tmp_tools):
    """Identity belongs to the gateway; a tool must not be able to accept one."""
    from app.tools import declarative

    (tmp_tools / "spoof.md").write_text(
        "---\nname: spoof\nurl: https://api.example.com/x\n"
        "params:\n  user_id: {type: string}\n  q: {type: string}\n---\nx",
        encoding="utf-8")
    props = declarative._load_http_tools()["spoof"].schema["function"]["parameters"]["properties"]
    assert "user_id" not in props and "q" in props


def test_declarative_tool_cannot_shadow_a_host_tool(tmp_tools):
    from app import toolsets

    (tmp_tools / "shell.md").write_text(
        "---\nname: shell\nurl: https://api.example.com/x\n---\nnope", encoding="utf-8")
    tools, dispatch = toolsets.load_for("custom", host_tools=False)
    assert "shell" not in {t["function"]["name"] for t in (tools or [])}
    assert "error" in asyncio.run(dispatch("shell", "u", {}))


def test_omitted_argument_drops_its_key(tmp_tools):
    """An optional parameter the model didn't supply must not be sent as "",
    which the backend would take as a value and use instead of its default."""
    from app.tools import declarative

    (tmp_tools / "t.md").write_text(
        "---\nname: t\nurl: https://api.example.com/x\n"
        "body:\n  query: '{{query}}'\n  limit: '{{limit}}'\n"
        "params:\n  query: {type: string}\n  limit: {type: integer}\n---\nx",
        encoding="utf-8")
    tool = declarative._load_http_tools()["t"]
    assert declarative._fill(tool.meta["body"], {"query": "hi"}) == {"query": "hi"}
    # …and a supplied one keeps its type, so `limit` arrives as a number.
    assert declarative._fill(tool.meta["body"], {"query": "hi", "limit": 3}) == \
        {"query": "hi", "limit": 3}


def test_env_var_in_url_is_expanded_at_load(tmp_tools, monkeypatch):
    from app.tools import declarative

    monkeypatch.setenv("TEST_BACKEND", "https://backend.example.com")
    (tmp_tools / "e.md").write_text(
        "---\nname: e\nurl: ${TEST_BACKEND}/api/x\n---\nx", encoding="utf-8")
    assert declarative._load_http_tools()["e"].url == "https://backend.example.com/api/x"


def test_url_with_an_unset_env_var_is_rejected(tmp_tools):
    """Better to lose the tool loudly at startup than to fail every call."""
    from app.tools import declarative

    (tmp_tools / "e.md").write_text(
        "---\nname: e\nurl: ${DEFINITELY_NOT_SET}/api/x\n---\nx", encoding="utf-8")
    assert "e" not in declarative._load_http_tools()


def test_tools_can_claim_their_own_toolset(tmp_tools):
    """A tool tagged `toolset:` stays out of the default `custom` bucket, so a
    pack's tools never appear in an agent that didn't ask for them."""
    from app import toolsets

    (tmp_tools / "mine.md").write_text(
        "---\nname: mine\ntoolset: acme\nurl: https://api.example.com/x\n---\nx", encoding="utf-8")
    (tmp_tools / "shared.md").write_text(
        "---\nname: shared\nurl: https://api.example.com/y\n---\ny", encoding="utf-8")

    acme, _ = toolsets.load_for("acme")
    custom, _ = toolsets.load_for("custom")
    assert {t["function"]["name"] for t in acme} == {"mine"}
    assert "mine" not in {t["function"]["name"] for t in custom}
    assert "shared" in {t["function"]["name"] for t in custom}
    assert toolsets.load_for("nobody_claims_this")[0] is None


# ── integration packs ────────────────────────────────────────────────────────

def test_pack_contributes_agents_and_tools(tmp_pack):
    from app import agents_store, toolsets
    from tests.conftest import write_agent

    pack, own = tmp_pack
    write_agent(own / "agents", "assistant", "name: assistant")
    write_agent(pack / "agents", "qubi", "name: qubi\ntoolset: [acme]")
    (pack / "tools" / "packed.md").write_text(
        "---\nname: packed\ntoolset: acme\nurl: https://api.example.com/x\n---\nx",
        encoding="utf-8")

    agents = agents_store.store().all()
    assert set(agents) == {"assistant", "qubi"}
    assert agents["qubi"].toolset == "acme"
    assert {t["function"]["name"] for t in toolsets.load_for("acme")[0]} == {"packed"}


def test_a_pack_cannot_silently_replace_an_existing_agent(tmp_pack):
    """First directory wins. A pack quietly shadowing the assistant an operator
    already runs is an outage that looks like nothing happened."""
    from app import agents_store
    from tests.conftest import write_agent

    pack, own = tmp_pack
    write_agent(own / "agents", "assistant", "name: assistant\ndescription: mine")
    write_agent(pack / "agents", "assistant", "name: assistant\ndescription: theirs")

    assert agents_store.store().all()["assistant"].description == "mine"


def test_pack_python_tools_load_and_may_define_several(tmp_pack):
    from app import toolsets

    pack, _ = tmp_pack
    (pack / "tools" / "custom" / "pair.py").write_text(
        'TOOLSET = "acme"\n'
        'def _schema(n):\n'
        '    return {"type": "function", "function": {"name": n, "description": n,\n'
        '            "parameters": {"type": "object", "properties": {}}}}\n'
        'async def _one(user_id, args): return {"who": "one"}\n'
        'async def _two(user_id, args): return {"who": "two"}\n'
        'TOOLS = [(_schema("one"), _one), (_schema("two"), _two)]\n',
        encoding="utf-8")

    tools, dispatch = toolsets.load_for("acme")
    assert {t["function"]["name"] for t in tools} == {"one", "two"}
    assert asyncio.run(dispatch("two", "u", {})) == {"who": "two"}


def test_pack_python_tool_can_import_its_own_helper(tmp_pack):
    """`_`-prefixed files are helpers, not tools — and must be importable."""
    from app import toolsets

    pack, _ = tmp_pack
    custom = pack / "tools" / "custom"
    (custom / "_shared.py").write_text("ANSWER = 42\n", encoding="utf-8")
    (custom / "uses_helper.py").write_text(
        'from _shared import ANSWER\n'
        'TOOLSET = "acme"\n'
        'SCHEMA = {"type": "function", "function": {"name": "helped", "description": "x",\n'
        '          "parameters": {"type": "object", "properties": {}}}}\n'
        'async def handler(user_id, args): return {"answer": ANSWER}\n',
        encoding="utf-8")

    tools, dispatch = toolsets.load_for("acme")
    assert {t["function"]["name"] for t in tools} == {"helped"}
    assert asyncio.run(dispatch("helped", "u", {})) == {"answer": 42}


# ── prompt cache breakpoint ──────────────────────────────────────────────────

def test_cache_breakpoint_splits_stable_from_volatile():
    from app import llm

    messages = [{"role": "system", "content": "PERSONA" + llm.CLOCK_PREFIX + "12:00 UTC."}]
    tools = [{"function": {"name": "a"}}, {"function": {"name": "b"}}]
    out_msgs, out_tools = llm._apply_cache_breakpoint(messages, tools)

    blocks = out_msgs[0]["content"]
    assert len(blocks) == 2
    assert "cache_control" in blocks[0] and "cache_control" not in blocks[1]
    assert blocks[0]["text"] == "PERSONA"
    assert "cache_control" in out_tools[-1] and "cache_control" not in out_tools[0]
    # inputs untouched, because the caller reuses them across tool turns
    assert isinstance(messages[0]["content"], str)
    assert "cache_control" not in tools[-1]


def test_only_explicit_cache_providers_are_targeted():
    from app import llm

    assert "anthropic/claude-sonnet-5".startswith(llm._EXPLICIT_CACHE_PREFIXES)
    assert not "openai/gpt-4o".startswith(llm._EXPLICIT_CACHE_PREFIXES)


def test_usage_normalises_across_providers():
    from app import llm

    class OpenAIish:
        prompt_tokens, completion_tokens, total_tokens = 100, 20, 120
        prompt_tokens_details = type("D", (), {"cached_tokens": 80})()

    usage = llm._usage_dict(OpenAIish())
    assert usage["cached_tokens"] == 80 and usage["total_tokens"] == 120

    class Anthropicish:
        prompt_tokens, completion_tokens, total_tokens = 50, 10, 0
        cache_read_input_tokens = 40

    usage = llm._usage_dict(Anthropicish())
    assert usage["cached_tokens"] == 40
    assert usage["total_tokens"] == 60          # derived when the provider omits it


# ── rate limiter ─────────────────────────────────────────────────────────────

def test_rate_limiter_allows_then_blocks():
    from app.ratelimit import RateLimiter

    limiter = RateLimiter(per_minute=60, burst=5)
    assert all(limiter.check("alice")[0] for _ in range(5))
    allowed, retry = limiter.check("alice")
    assert allowed is False and retry > 0


def test_rate_limiter_is_per_user():
    from app.ratelimit import RateLimiter

    limiter = RateLimiter(per_minute=60, burst=2)
    limiter.check("alice"); limiter.check("alice")
    assert limiter.check("alice")[0] is False
    assert limiter.check("bob")[0] is True       # one user cannot starve another


def test_rate_limiter_disabled_by_zero():
    from app.ratelimit import RateLimiter

    limiter = RateLimiter(per_minute=0)
    assert all(limiter.check("anyone")[0] for _ in range(100))


def test_rate_limiter_prunes_idle_users():
    from app.ratelimit import RateLimiter

    limiter = RateLimiter(per_minute=60)
    limiter.check("ghost")
    assert limiter.tracked == 1
    assert limiter.prune(max_idle=-1) == 1       # a long-lived worker must not leak
    assert limiter.tracked == 0


# ── named api keys ───────────────────────────────────────────────────────────

def test_named_keys_parse():
    from app.config import _parse_keys

    keys = _parse_keys("web:abc123, mobile:def456")
    assert keys == {"abc123": "web", "def456": "mobile"}
    assert _parse_keys("") == {}
    assert _parse_keys("nocolon") == {}          # malformed entries are ignored


# ── metrics ──────────────────────────────────────────────────────────────────

def test_histogram_buckets_are_cumulative():
    from app import metrics

    for value in (0.3, 1.5, 4.0):
        metrics.observe("proteus_tool_duration_seconds", value, tool="t")
    text = metrics.render()
    rows = [l for l in text.splitlines()
            if l.startswith("proteus_tool_duration_seconds_bucket") and 'tool="t"' in l]
    counts = [int(r.rsplit(" ", 1)[1]) for r in rows]
    assert counts == sorted(counts), "buckets must be non-decreasing"
    assert counts[-1] == 3


def test_metrics_escapes_label_values():
    from app import metrics

    metrics.inc("proteus_test_total", 1, tenant='say "hi"')
    assert '\\"hi\\"' in metrics.render()
