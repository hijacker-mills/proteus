"""
The "agent" toolset — general capabilities for a do-things assistant.

Exposes web_search, web_fetch, and (when enabled) run_code in OpenAI function
format, with a dispatch(name, user_id, args) matching the agent loop's contract.
Add a tool by writing a handler + a schema entry here.
"""
from __future__ import annotations

from typing import Any

from .. import config
from . import browser, code, email as email_tool, schedule as schedule_tool, shell as shell_tool, web


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


_BASE_TOOLS = [
    _fn(
        "web_search",
        "Search the web for current information. Use for anything recent, factual, "
        "or that you're unsure about. Returns titles, URLs, and snippets.",
        {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        ["query"],
    ),
    _fn(
        "web_fetch",
        "Fetch and read the main text content of a web page (e.g. a result from "
        "web_search). Use to get details before answering.",
        {"url": {"type": "string", "description": "Absolute http(s) URL to read"}},
        ["url"],
    ),
]

_CODE_TOOL = _fn(
    "run_code",
    "Execute a short Python 3 snippet and return its stdout/stderr. Use for "
    "calculations, data transforms, quick scripting. print() what you want back.",
    {"code": {"type": "string", "description": "Python source to run"}},
    ["code"],
)

_BROWSER_TOOL = _fn(
    "browser",
    "Drive a real web browser for pages that web_search/web_fetch can't handle — "
    "JS apps, logins, multi-step flows. Start with action='navigate', then 'read' "
    "to see the page (returns text + links/buttons/inputs), then 'click'/'fill'/"
    "'type'/'press' to interact (these auto-return the updated page). Use CSS "
    "selectors, or 'click_text' for a visible link/button label.",
    {
        "action": {"type": "string",
                   "enum": ["navigate", "read", "click", "click_text", "fill", "type", "press", "back", "eval"]},
        "url": {"type": "string", "description": "URL for navigate"},
        "selector": {"type": "string", "description": "CSS selector for click/fill/type"},
        "text": {"type": "string", "description": "value to fill/type, or label for click_text"},
        "key": {"type": "string", "description": "key for press, e.g. Enter"},
        "code": {"type": "string", "description": "JS for eval"},
    },
    ["action"],
)

_SHELL_TOOL = _fn(
    "shell",
    "Run a shell command on the host (bash). Use for any CLI tool — git, file ops, "
    "aws, hf (huggingface), himalaya, package managers, etc. Returns stdout/stderr/exit code.",
    {"command": {"type": "string", "description": "The shell command to run"}},
    ["command"],
)

_EMAIL_TOOL = _fn(
    "email",
    "Read and send email via the himalaya CLI (configured accounts). list/search/read "
    "are safe; send dispatches a real email — confirm with the user first.",
    {
        "action": {"type": "string", "enum": ["list", "search", "read", "send"]},
        "to": {"type": "string", "description": "recipient (send)"},
        "subject": {"type": "string", "description": "subject (send)"},
        "body": {"type": "string", "description": "body (send)"},
        "id": {"type": "string", "description": "message id (read)"},
        "query": {"type": "string", "description": "search query (search)"},
        "folder": {"type": "string", "description": "mailbox folder, default INBOX"},
        "account": {"type": "string", "description": "account name, e.g. gmail; omit for default"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
    },
    ["action"],
)

_SCHEDULE_TOOL = _fn(
    "schedule",
    "Schedule a task to run later and deliver its result to this chat — reminders, "
    "digests, recurring jobs. For recurring use a cron expression in 'cron_expr' "
    f"(interpreted in {config.CRON_TZ}; e.g. '0 8 * * *' = 08:00 daily, '*/30 * * * *' "
    "= every 30 min). For a one-off use 'in_seconds' (e.g. 7200 = in 2 hours). The "
    "'prompt' is an instruction to yourself to carry out when it fires (you'll have "
    "your tools then). action=list shows jobs; action=cancel with id removes one.",
    {
        "action": {"type": "string", "enum": ["create", "list", "cancel"]},
        "prompt": {"type": "string", "description": "what to do when it runs (for create)"},
        "cron_expr": {"type": "string", "description": "cron expression for a recurring job"},
        "in_seconds": {"type": "integer", "description": "run once after this many seconds"},
        "job_id": {"type": "integer", "description": "job id (for cancel)"},
    },
    ["action"],
)

TOOLS = (
    _BASE_TOOLS
    + ([_BROWSER_TOOL] if config.TOOLS_BROWSER else [])
    + ([_CODE_TOOL] if config.TOOLS_CODE_EXEC else [])
    + ([_SHELL_TOOL] if config.TOOLS_SHELL else [])
    + ([_EMAIL_TOOL] if config.TOOLS_EMAIL else [])
    + ([_SCHEDULE_TOOL] if config.CRON_ENABLED else [])
)


async def dispatch(name: str, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    args = args or {}
    if name == "web_search":
        return await web.web_search(args.get("query", ""), int(args.get("count", 5) or 5))
    if name == "web_fetch":
        return await web.web_fetch(args.get("url", ""))
    if name == "run_code":
        return await code.run_code(args.get("code", ""))
    if name == "browser":
        return await browser.browser(
            args.get("action", ""),
            url=args.get("url"), selector=args.get("selector"),
            text=args.get("text"), key=args.get("key"), code=args.get("code"),
        )
    if name == "shell":
        return await shell_tool.shell(args.get("command", ""))
    if name == "email":
        return await email_tool.email(
            args.get("action", ""),
            to=args.get("to"), subject=args.get("subject"), body=args.get("body"),
            id=args.get("id"), query=args.get("query"),
            folder=args.get("folder", "INBOX"), account=args.get("account"),
            limit=int(args.get("limit", 10) or 10),
        )
    if name == "schedule":
        return await schedule_tool.schedule(
            user_id, args.get("action", ""),
            prompt=args.get("prompt"), cron_expr=args.get("cron_expr"),
            in_seconds=args.get("in_seconds"), job_id=args.get("job_id"),
        )
    return {"error": f"unknown tool: {name}"}
