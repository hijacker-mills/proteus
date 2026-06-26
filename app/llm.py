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
import os
from typing import Any, AsyncGenerator

import litellm

from . import config

# Keep the gateway quiet and resilient across heterogeneous providers.
litellm.telemetry = False
litellm.drop_params = True          # silently drop params a given provider doesn't accept
litellm.suppress_debug_info = True
os.environ.setdefault("LITELLM_LOG", "ERROR")


async def astream(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
    reasoning_effort: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """
    Stream one model completion. Yields normalized events:
        {"type": "text", "text": "..."}                      incremental text
        {"type": "final", "tool_calls": [...], "finish_reason": "..."}   end of turn

    Each tool call is {"id": str, "name": str, "arguments": dict}.
    """
    # Codex (ChatGPT OAuth) is not a LiteLLM provider — route it to our own
    # Responses-API client. Same normalized event contract, so callers don't care.
    if model.startswith("codex/") or model.startswith("openai-codex/"):
        from . import codex_provider

        async for ev in codex_provider.astream(
            model.split("/", 1)[1], messages, tools, max_tokens, temperature, reasoning_effort
        ):
            yield ev
        return

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    response = await litellm.acompletion(**kwargs)

    # tool_calls arrive fragmented across chunks (OpenAI streaming semantics);
    # accumulate by index, concatenating the argument-string deltas.
    acc: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None

    async for chunk in response:
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

    yield {"type": "final", "tool_calls": tool_calls, "finish_reason": finish_reason}


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
