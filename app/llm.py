"""
Provider-neutral LLM engine — the ONLY module that knows about model backends.

Built on LiteLLM, which speaks each provider's native API (Anthropic, OpenAI,
OpenRouter, Groq, Mistral, Gemini, local Ollama / vLLM, …) while exposing one
unified streaming + tool-calling interface in OpenAI shape. Switching models is
a single env change — `MODEL=anthropic/claude-sonnet-4-6`, `openai/gpt-4o-mini`,
`ollama/llama3.1`, `openrouter/anthropic/claude-3.5-sonnet`, etc. — provided the
matching provider key is in the environment.

To swap LiteLLM out entirely, replace just this file; the agent loop depends
only on `astream()` and its normalized event shape.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncGenerator

import litellm

from . import config

logger = logging.getLogger("proteus.llm")

# Keep the gateway quiet and resilient across heterogeneous providers.
litellm.telemetry = False
litellm.drop_params = True          # silently drop params a given provider doesn't accept
litellm.suppress_debug_info = True
os.environ.setdefault("LITELLM_LOG", "ERROR")

# The volatile tail of the system prompt. agent._prepare() appends the clock
# last so everything before it is a byte-stable prefix; this marker is how the
# two halves are told apart again when a provider needs an explicit cache
# breakpoint. Defined here (not in agent.py) because agent.py imports llm, and
# the reverse would be a cycle.
CLOCK_PREFIX = "\n\nCurrent time: "

# Providers that cache on an explicit breakpoint rather than automatic prefix
# matching. Everyone else (OpenAI, the Codex Responses backend, Gemini) caches
# a stable prefix on its own and needs nothing from us.
_EXPLICIT_CACHE_PREFIXES = ("anthropic/", "bedrock/anthropic", "vertex_ai/claude")


def _apply_cache_breakpoint(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None):
    """Mark the stable head of the system prompt (and the tool schemas) cacheable.

    Anthropic caches only up to an explicit `cache_control` breakpoint, so the
    marker goes on the STABLE half of the system prompt. Marking the whole thing
    would include the clock and re-write the cache every minute, which costs
    more than it saves.

    Returns new lists; the originals are left alone because the caller may reuse
    them across tool-loop turns.
    """
    out_msgs = list(messages)
    for i, m in enumerate(out_msgs):
        if m.get("role") != "system" or not isinstance(m.get("content"), str):
            continue
        stable, sep, volatile = m["content"].rpartition(CLOCK_PREFIX)
        blocks = (
            [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
             {"type": "text", "text": sep + volatile}]
            if sep else
            [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}]
        )
        out_msgs[i] = {**m, "content": blocks}
        break

    out_tools = tools
    if tools:
        # One breakpoint on the LAST schema covers every tool before it.
        out_tools = list(tools)
        out_tools[-1] = {**out_tools[-1], "cache_control": {"type": "ephemeral"}}
    return out_msgs, out_tools


def _usage_dict(usage: Any) -> dict[str, int]:
    """Normalise a provider's usage object into flat ints.

    Providers disagree on the shape: cached tokens live under
    `prompt_tokens_details.cached_tokens` for OpenAI and as top-level
    `cache_read_input_tokens` for Anthropic, so both are checked.
    """
    def num(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    out = {
        "prompt_tokens": num(getattr(usage, "prompt_tokens", None)),
        "completion_tokens": num(getattr(usage, "completion_tokens", None)),
        "total_tokens": num(getattr(usage, "total_tokens", None)),
    }
    details = getattr(usage, "prompt_tokens_details", None)
    cached = num(getattr(details, "cached_tokens", None)) if details else 0
    cached = cached or num(getattr(usage, "cache_read_input_tokens", None))
    if cached:
        out["cached_tokens"] = cached
    written = num(getattr(usage, "cache_creation_input_tokens", None))
    if written:
        out["cache_write_tokens"] = written
    if not out["total_tokens"]:
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


async def astream(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
    reasoning_effort: str | None = None,
    reasoning_max_tokens: int | None = None,
    tool_choice: str = "auto",
) -> AsyncGenerator[dict[str, Any], None]:
    """
    Stream one model completion. Yields normalized events:
        {"type": "text", "text": "..."}                      incremental text
        {"type": "final", "tool_calls": [...], "finish_reason": "...",
         "usage": {...}}                                     end of turn

    Each tool call is {"id": str, "name": str, "arguments": dict}.

    `usage` carries prompt/completion/cached token counts when the provider
    reports them. Without it the gateway cannot say what a user or an agent
    cost, which for a shared provider account is the first question asked when
    the bill arrives.

    `tool_choice="none"` asks for an answer with no tool call. The tools are
    still sent where the provider takes the flag, because they sit in the
    cached prompt prefix and dropping them would miss the cache on the one call
    that most needs to be quick. Providers routed outside LiteLLM get no tools.
    """
    if tool_choice == "none" and model.startswith(("codex/", "openai-codex/", "mock/")):
        tools = None

    # Codex (ChatGPT OAuth) is not a LiteLLM provider — route it to our own
    # Responses-API client. Same normalized event contract, so callers don't care.
    if model.startswith("codex/") or model.startswith("openai-codex/"):
        from . import codex_provider

        async for ev in codex_provider.astream(
            model.split("/", 1)[1], messages, tools, max_tokens, temperature, reasoning_effort
        ):
            yield ev
        return

    # Kimi Code: OpenAI-compatible wire format, own host and key. Rewritten to
    # openai/* with an explicit base so it cannot collide with real OpenAI use.
    if model.startswith("kimi-code/"):
        if not config.KIMI_CODE_API_KEY:
            raise RuntimeError("KIMI_CODE_API_KEY is not set — run: proteus auth login -p kimi-code")
        model = "openai/" + model.split("/", 1)[1]
        extra_kwargs = {"api_base": config.KIMI_CODE_API_BASE,
                        "api_key": config.KIMI_CODE_API_KEY,
                        # These models reject any temperature but 1, and reject
                        # it as a hard 400 rather than clamping. drop_params
                        # cannot help: the parameter is supported, the VALUE is
                        # not, so it has to be overridden here.
                        "temperature": 1}
    else:
        extra_kwargs = {}

    # Synthetic backend for load-testing the gateway with no provider involved.
    if model.startswith("mock/"):
        from . import mock_provider

        async for ev in mock_provider.astream(
            model.split("/", 1)[1], messages, tools, max_tokens, temperature, reasoning_effort
        ):
            yield ev
        return

    if config.PROMPT_CACHING and model.startswith(_EXPLICIT_CACHE_PREFIXES):
        messages, tools = _apply_cache_breakpoint(messages, tools)

    # OpenRouter fronts several providers per model and picks one per request,
    # and they are NOT interchangeable on speed. Measured on
    # z-ai/glm-5.3-flash, same prompt, same params: 0.6-0.8s when Baseten
    # served it and 15-17s when a slower provider did, with the unpinned
    # default sitting around 10s. That spread is the single biggest lever on
    # how Qubi feels, so it is worth naming the provider you want.
    #
    # `allow_fallbacks` defaults ON deliberately. Pinning hard with fallbacks
    # off returns a bare 429 the moment the preferred provider's shared pool is
    # rate-limited, which it demonstrably is:
    #   "z-ai/glm-5.3-flash is temporarily rate-limited upstream"
    # A slow answer beats no answer. To get the fast path consistently, attach
    # your own provider key in OpenRouter's integrations settings; that leaves
    # the shared pool and its limits behind.
    if model.startswith("openrouter/"):
        routing: dict[str, Any] = {}
        if config.OPENROUTER_PROVIDER_ORDER:
            routing["order"] = config.OPENROUTER_PROVIDER_ORDER
        if config.OPENROUTER_PROVIDER_SORT:
            routing["sort"] = config.OPENROUTER_PROVIDER_SORT
        extra_body: dict[str, Any] = {}
        if routing:
            routing["allow_fallbacks"] = config.OPENROUTER_ALLOW_FALLBACKS
            extra_body["provider"] = routing
        # Bound or steer reasoning. OpenRouter accepts only ONE of
        # reasoning.effort / reasoning.max_tokens — sending both is HTTP 400.
        # effort wins when the agent asks for both.
        if reasoning_effort:
            extra_body["reasoning"] = {"effort": reasoning_effort}
        elif reasoning_max_tokens:
            extra_body["reasoning"] = {"max_tokens": reasoning_max_tokens}
        if extra_body:
            extra_kwargs = {**extra_kwargs, "extra_body": extra_body}
    elif reasoning_effort:
        # Non-OpenRouter providers that understand the unified effort param.
        extra_kwargs = {**extra_kwargs, "reasoning_effort": reasoning_effort}

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Without an explicit timeout a stalled provider connection pins this
        # request, its SSE stream and a slot in the concurrency limiter forever.
        "timeout": config.REQUEST_TIMEOUT,
        # Transient 429/5xx are retried by LiteLLM with backoff. This is a
        # courtesy for blips, NOT a substitute for capacity: sustained 429 means
        # the provider tier is too small, and retries will only deepen the queue.
        "num_retries": config.LLM_RETRIES,
        # Streaming responses omit usage unless it is asked for explicitly, and
        # the final chunk is the only place it appears.
        "stream_options": {"include_usage": True},
        **extra_kwargs,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    response = await litellm.acompletion(**kwargs)

    # tool_calls arrive fragmented across chunks (OpenAI streaming semantics);
    # accumulate by index, concatenating the argument-string deltas.
    acc: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, int] = {}

    async for chunk in response:
        # The usage chunk carries no choices, so read it before skipping those.
        if getattr(chunk, "usage", None):
            usage = _usage_dict(chunk.usage)
        choices = getattr(chunk, "choices", None)
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue

        text = getattr(delta, "content", None)
        if text:
            yield {"type": "text", "text": text}

        for tc in (getattr(delta, "tool_calls", None) or []):
            idx = getattr(tc, "index", 0) or 0
            slot = acc.setdefault(idx, {"id": None, "name": None, "args": ""})
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["args"] += fn.arguments

        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason

    tool_calls = []
    for idx in sorted(acc):
        slot = acc[idx]
        if not slot["name"]:
            continue
        try:
            parsed = json.loads(slot["args"]) if slot["args"].strip() else {}
        except (json.JSONDecodeError, ValueError):
            parsed = {}
        tool_calls.append({
            "id": slot["id"] or f"call_{idx}",
            "name": slot["name"],
            "arguments": parsed,
        })

    yield {"type": "final", "tool_calls": tool_calls,
           "finish_reason": finish_reason, "usage": usage, "model": model}


async def astream_chain(
    models: list[str],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
    reasoning_effort: str | None = None,
    reasoning_max_tokens: int | None = None,
    tool_choice: str = "auto",
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream `models[0]`, falling through to the next one if it fails.

    Same normalized event contract as `astream`; this only decides WHICH model
    produces them.

    THE RULE: a model is abandoned only if it fails before yielding anything.
    Once one event is out, the caller has it — the HTTP layer has already
    flushed those tokens to the student's screen — and re-running the turn on
    another model would either duplicate the text or contradict it mid-sentence.
    So a mid-stream failure propagates, exactly as it did before fallbacks
    existed. Fallbacks buy availability at connect time, not a redo.

    Deliberately not LiteLLM's own `fallbacks=` kwarg: that routes through
    `completion_with_fallbacks`, whose behaviour around streaming and our
    normalized events varies by version. Being explicit here makes the rule
    above readable and testable, and keeps `astream` a single-model primitive.

    `astream` is looked up as a module global on each attempt, so tests that
    monkeypatch `llm.astream` still intercept every call.
    """
    chain = [m for m in models if m]
    if not chain:
        chain = [config.MODEL]

    for index, model in enumerate(chain):
        started = False
        try:
            async for event in astream(
                model=model,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                reasoning_max_tokens=reasoning_max_tokens,
                tool_choice=tool_choice,
            ):
                started = True
                yield event
            return
        except Exception as exc:
            remaining = chain[index + 1:]
            if started or not remaining:
                raise
            logger.warning(
                "model %s failed before producing output (%s: %s) — falling back to %s",
                model, type(exc).__name__, str(exc)[:200], remaining[0],
            )


async def close() -> None:
    """Best-effort cleanup of LiteLLM's async HTTP clients (version-tolerant)."""
    for fn_name in ("close_litellm_async_clients", "aclose"):
        fn = getattr(litellm, fn_name, None)
        if fn is None:
            continue
        try:
            res = fn()
            if hasattr(res, "__await__"):
                await res
        except Exception:
            pass
