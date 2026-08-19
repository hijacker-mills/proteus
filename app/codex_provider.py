"""
OpenAI Codex backend provider — the ChatGPT Responses API at
{CODEX_BASE_URL}/responses, authenticated with the OAuth token from codex_auth.

Exposes the SAME normalized streaming contract as llm.astream():
    {"type": "text", "text": ...}
    {"type": "final", "tool_calls": [...], "finish_reason": ...}

so app/agent.py is unaware of which backend served the turn. It converts the
gateway's OpenAI-chat-format messages/tools into Responses "input" items and
function tools, and parses the Responses SSE event stream back.

The codex_cli_rs User-Agent + originator headers are REQUIRED — Cloudflare 403s
the backend from non-residential IPs without them.
"""
from __future__ import annotations

import json
from typing import Any, AsyncGenerator

from . import codex_auth, config
from .httpclient import get_stream_client


def _to_input_content(content: Any) -> list[dict]:
    """OpenAI message content (str or multimodal parts) → Responses input parts."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts: list[dict] = []
    for p in content or []:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            parts.append({"type": "input_text", "text": p.get("text", "")})
        elif p.get("type") == "image_url":
            iu = p.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if url:
                parts.append({"type": "input_image", "image_url": url})
    return parts or [{"type": "input_text", "text": ""}]


def _convert(messages: list[dict[str, Any]]) -> tuple[str, list[dict]]:
    """OpenAI chat messages → (instructions, Responses input items)."""
    instructions: list[str] = []
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            if isinstance(content, str) and content.strip():
                instructions.append(content.strip())
        elif role == "user":
            items.append({"role": "user", "content": _to_input_content(content)})
        elif role == "assistant":
            if content.strip():
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": content}]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments", "{}"),
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id"),
                "output": content,
            })
    return "\n\n".join(instructions), items


def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({
            "type": "function",
            "name": fn.get("name"),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
            "strict": False,
        })
    return out


async def astream(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    temperature: float,
    reasoning_effort: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    access, account = await codex_auth.get_auth()
    instructions, input_items = _convert(messages)

    body: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "store": False,
        "stream": True,
    }
    conv_tools = _convert_tools(tools)
    if conv_tools:
        body["tools"] = conv_tools
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = True
    effort = reasoning_effort if reasoning_effort is not None else config.CODEX_REASONING_EFFORT
    if effort and effort.lower() not in ("none", "off"):
        body["reasoning"] = {"effort": effort, "summary": "auto"}
        body["include"] = ["reasoning.encrypted_content"]

    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.0.0",
        "originator": "codex_cli_rs",
    }
    if account:
        headers["ChatGPT-Account-ID"] = account

    url = f"{config.CODEX_BASE_URL}/responses"
    # function_call items assembled across the stream, keyed by item id.
    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    finish = "stop"
    usage: dict[str, int] = {}

    # Pooled, process-wide client. Opening one per completion re-handshakes TLS
    # on every turn and discards the pool, which shows up as latency under load.
    client = get_stream_client()
    async with client.stream("POST", url, json=body, headers=headers) as resp:
        if resp.status_code != 200:
            detail = (await resp.aread()).decode("utf-8", "replace")[:300]
            raise RuntimeError(f"codex responses HTTP {resp.status_code}: {detail}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            etype = ev.get("type", "")

            if etype == "response.output_text.delta":
                delta = ev.get("delta")
                if delta:
                    yield {"type": "text", "text": delta}

            elif etype == "response.output_item.added":
                item = ev.get("item", {})
                if item.get("type") == "function_call":
                    iid = item.get("id") or item.get("call_id")
                    calls[iid] = {
                        "id": item.get("call_id") or iid,
                        "name": item.get("name"),
                        "args": item.get("arguments") or "",
                    }
                    order.append(iid)

            elif etype == "response.function_call_arguments.delta":
                iid = ev.get("item_id")
                if iid in calls:
                    calls[iid]["args"] += ev.get("delta", "")

            elif etype == "response.output_item.done":
                item = ev.get("item", {})
                if item.get("type") == "function_call":
                    iid = item.get("id") or item.get("call_id")
                    slot = calls.setdefault(iid, {"id": item.get("call_id") or iid, "name": None, "args": ""})
                    if iid not in order:
                        order.append(iid)
                    slot["id"] = item.get("call_id") or slot["id"]
                    slot["name"] = item.get("name") or slot["name"]
                    if item.get("arguments"):
                        slot["args"] = item["arguments"]

            elif etype in ("response.completed", "response.failed", "response.incomplete"):
                if etype == "response.failed":
                    err = (ev.get("response", {}) or {}).get("error", {})
                    raise RuntimeError(f"codex response failed: {err.get('message', err)}")
                # The Responses API reports usage on the terminal event, under
                # its own names (input/output rather than prompt/completion).
                u = (ev.get("response") or {}).get("usage") or {}
                if u:
                    usage = {
                        "prompt_tokens": int(u.get("input_tokens") or 0),
                        "completion_tokens": int(u.get("output_tokens") or 0),
                        "total_tokens": int(u.get("total_tokens") or 0),
                    }
                    cached = int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)
                    if cached:
                        usage["cached_tokens"] = cached
                    if not usage["total_tokens"]:
                        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
                break

            elif etype == "error":
                raise RuntimeError(f"codex stream error: {ev.get('message', ev)}")

    tool_calls = []
    for iid in order:
        slot = calls.get(iid)
        if not slot or not slot.get("name"):
            continue
        try:
            args = json.loads(slot["args"]) if slot["args"].strip() else {}
        except (json.JSONDecodeError, ValueError):
            args = {}
        tool_calls.append({"id": slot["id"], "name": slot["name"], "arguments": args})

    if tool_calls:
        finish = "tool_calls"
    yield {"type": "final", "tool_calls": tool_calls, "finish_reason": finish, "usage": usage}
