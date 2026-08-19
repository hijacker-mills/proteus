"""Environment-driven configuration. Loaded once at import."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


# Client auth
API_KEY = os.environ.get("API_KEY", "").strip()
# Host-access tools (shell, run_code, email, schedule) are NEVER available over
# HTTP unless the caller also presents this key in X-Proteus-Admin-Key. Empty
# (the default) means: no HTTP caller can ever reach them, whatever profile they
# select. API_KEY alone must not be enough — it is typically shared with every
# product surface that talks to the gateway, so it would otherwise make host RCE
# reachable by anyone who obtains it. See toolsets.HOST_TOOLS.
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "").strip()

# Named keys, so each surface has its own and one can be revoked without
# rotating for everybody: API_KEYS="web:abc123,mobile:def456". The label is
# recorded in logs and metrics, which is how you find out WHICH consumer is
# responsible for a spike. API_KEY still works and is labelled "default".
def _parse_keys(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        label, sep, secret = pair.partition(":")
        if sep and secret.strip():
            out[secret.strip()] = label.strip() or "unnamed"
    return out


API_KEYS = _parse_keys(os.environ.get("API_KEYS", ""))
if API_KEY:
    API_KEYS.setdefault(API_KEY, "default")

# Per-user request ceiling, PER WORKER (so the real limit is this x WORKERS).
# 0 disables. The bucket permits a burst up to RATE_LIMIT_BURST, defaulting to
# one minute's worth, because a few rapid turns is normal use and a sustained
# flood is not.
RATE_LIMIT_PER_MINUTE = _int("RATE_LIMIT_PER_MINUTE", 0)
RATE_LIMIT_BURST = _int("RATE_LIMIT_BURST", 0) or None

# Server
HOST = os.environ.get("HOST", "0.0.0.0").strip()
PORT = _int("PORT", 18791)
WORKERS = _int("WORKERS", 4)

# Model (provider-prefixed, LiteLLM style: anthropic/…, openai/…, ollama/…, openrouter/…)
MODEL = os.environ.get("MODEL", "anthropic/claude-sonnet-4-6").strip()
MAX_TOKENS = _int("MAX_TOKENS", 1500)
MAX_TOOL_TURNS = _int("MAX_TOOL_TURNS", 8)
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3") or 0.3)
# Hard ceiling on one upstream completion. A reasoning model on a long answer can
# legitimately run minutes, so this is generous; its job is to stop a *stalled*
# connection holding a stream and a concurrency slot indefinitely.
REQUEST_TIMEOUT = _int("REQUEST_TIMEOUT", 300)
LLM_RETRIES = _int("LLM_RETRIES", 2)

# Backpressure. Caps upstream completions in flight PER WORKER process, so the
# real cap is this x WORKERS. Excess requests wait up to CONCURRENCY_WAIT for a
# slot, then get 503 + Retry-After rather than queueing without bound.
#
# Why cap at all: the gateway itself handles thousands of idle connections
# cheaply, but every in-flight completion consumes provider quota. Without a cap
# a traffic spike converts directly into provider 429s for EVERY user, including
# the ones already mid-stream. A cap degrades a spike into "some users wait",
# which is recoverable, instead of "everyone fails", which is not.
# 0 disables the limiter entirely.
MAX_CONCURRENT_COMPLETIONS = _int("MAX_CONCURRENT_COMPLETIONS", 64)
CONCURRENCY_WAIT = float(os.environ.get("CONCURRENCY_WAIT", "20") or 20)

# Tool calls within ONE turn run concurrently up to this many at a time. Models
# routinely emit several per turn, and running them in sequence made a turn cost
# the sum of its tools rather than the slowest one.
MAX_PARALLEL_TOOLS = _int("MAX_PARALLEL_TOOLS", 8)

# Batch streamed deltas into one SSE frame for this many ms. 0 = off (a frame
# per token, smoothest). A small value cuts syscalls and CPU under heavy
# concurrency at a barely perceptible cost to smoothness.
STREAM_COALESCE_MS = _int("STREAM_COALESCE_MS", 0)

# Send an explicit prompt-cache breakpoint to providers that need one (Anthropic
# via cache_control). Prefix-caching providers (OpenAI, the Codex Responses
# backend) cache automatically and ignore this.
PROMPT_CACHING = os.environ.get("PROMPT_CACHING", "true").lower() not in ("0", "false", "no")
# Provider API keys are read by LiteLLM directly from the environment
# (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, GROQ_API_KEY, …).
# python-dotenv has already loaded them; nothing else to wire up here.

# Kimi Code — Moonshot's coding endpoint. OpenAI-compatible but on its own host
# with its own key, so it cannot just be `openai/…`: pointing OPENAI_API_BASE at
# it would hijack every real OpenAI model too. MODEL=kimi-code/<model> routes
# here instead. `proteus auth login -p kimi-code` sets the key.
KIMI_CODE_API_KEY = os.environ.get("KIMI_CODE_API_KEY", "").strip()
KIMI_CODE_API_BASE = os.environ.get("KIMI_CODE_API_BASE", "https://api.kimi.com/coding/v1").rstrip("/")

# Prometheus endpoint. Behind API_KEY auth unless METRICS_PUBLIC — token counts
# and tenant labels describe your business, not just your servers.
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "true").lower() not in ("0", "false", "no")
METRICS_PUBLIC = os.environ.get("METRICS_PUBLIC", "false").lower() in ("1", "true", "yes")

# Postgres (optional; a pooled endpoint if your provider offers one)
# OPTIONAL, and it has to actually be optional: /v1/chat/completions never
# touches the database, so a gateway with no DATABASE_URL must start and serve
# chat rather than refuse to import. Requiring it here contradicted both db.py
# and the README, and made a fresh install crash before it could say why.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_POOL_MIN = _int("DB_POOL_MIN", 2)
DB_POOL_MAX = _int("DB_POOL_MAX", 20)

# Embeddings
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "mxbai-embed-large").strip()


# Optional integrations
IMAGE_SEARCH_SERVICE_URL = os.environ.get("IMAGE_SEARCH_SERVICE_URL", "").rstrip("/")
IMAGE_SEARCH_SERVICE_TOKEN = os.environ.get("IMAGE_SEARCH_SERVICE_TOKEN", "").strip()
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
TOOL_EVENT_STREAM = os.environ.get("TOOL_EVENT_STREAM", "proteus:tool-events").strip()

# ── Agent identity & tools ───────────────────────────────────────────────────
# SYSTEM_PROMPT_FILE is relative to the app/ package dir.
SYSTEM_PROMPT_FILE = os.environ.get("SYSTEM_PROMPT_FILE", "prompts/assistant.md").strip()
# TOOLSET: comma list of "none" | "web" | "agent" | "custom" (see app/toolsets.py)
TOOLSET = os.environ.get("TOOLSET", "none").strip()

# ── Agents (persona + toolset, selected per request via X-Proteus-Profile) ─────
# Definitions live in agents/*.md. This names the one to use when a request
# doesn't ask for a specific agent.
DEFAULT_PROFILE = os.environ.get("DEFAULT_PROFILE", "assistant").strip()
# Directory of agent definitions (agents/*.md). Empty = <repo>/agents. When it
# holds no definitions, one agent is assembled from SYSTEM_PROMPT_FILE + TOOLSET
# so the gateway still serves instead of refusing to start.
AGENTS_DIR = os.environ.get("AGENTS_DIR", "").strip()
# Directory of declarative HTTP tools (tools/*.md). Empty = <repo>/tools.
TOOLS_DIR = os.environ.get("TOOLS_DIR", "").strip()

# ── Agent tools ──────────────────────────────────────────────────────────────
# Search providers, tried in this order. With none set, web_search drives the
# real browser instead — scraping engines over plain HTTP now gets challenged.
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()
BRAVE_SEARCH_API_KEY = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "").strip()
SEARCH_URL_TEMPLATE = os.environ.get(
    "SEARCH_URL_TEMPLATE", "https://www.bing.com/search?q=").strip()
WEB_FETCH_MAX_CHARS = _int("WEB_FETCH_MAX_CHARS", 6000)
# Code execution is OFF by default — it runs arbitrary code on this host. Only
# enable AFTER restricting who can reach the bot (e.g. TELEGRAM_ALLOWED_USERS).
TOOLS_CODE_EXEC = os.environ.get("TOOLS_CODE_EXEC", "false").strip().lower() in ("1", "true", "yes")
TOOLS_CODE_TIMEOUT = _int("TOOLS_CODE_TIMEOUT", 12)
# Document root for the read_file / list_files tools. UNSET = the tools refuse
# outright, which is the safe default: with a root set they can read nothing
# outside it, which is what keeps them off the host-tool list.
# Where read_file/list_files may look. Colon-separated, so several roots are
# allowed. Empty = the tools refuse everything.
#
# `FILES_ROOT=/` means the whole filesystem, for a personal assistant whose user
# is the operator. That is a real privilege — it reads .env, ~/.ssh and any
# token file on the box — so in that mode the tools become HOST tools and are
# withheld from untrusted callers exactly like `shell` (see toolsets.host_tools).
FILES_ROOT = os.environ.get("FILES_ROOT", "").strip()
FILES_ROOTS = [r.strip() for r in FILES_ROOT.split(":") if r.strip()]
FILES_UNRESTRICTED = "/" in FILES_ROOTS
FILES_MAX_BYTES = _int("FILES_MAX_BYTES", 1_000_000)

# SSRF guard. Tools that fetch a model-chosen URL refuse private and metadata
# addresses. Cloud metadata endpoints stay blocked even when this is true — see
# app/tools/url_safety.py.
ALLOW_PRIVATE_URLS = os.environ.get("ALLOW_PRIVATE_URLS", "false").strip().lower() in ("1", "true", "yes")

# Extra directories prepended to PATH for the shell/email tools, so CLIs installed
# outside the system path (nvm, pyenv, ~/.local/bin) resolve. Colon-separated.
TOOLS_EXTRA_PATH = os.environ.get(
    "TOOLS_EXTRA_PATH", os.path.expanduser("~/.local/bin")).strip()

# Browser tool — a real headless Chromium in-process, via Playwright.
TOOLS_BROWSER = os.environ.get("TOOLS_BROWSER", "true").strip().lower() in ("1", "true", "yes")
TOOLS_BROWSER_TIMEOUT = _int("TOOLS_BROWSER_TIMEOUT", 45)
# Shell tool — runs arbitrary commands (every CLI: himalaya, aws, git, hf, …). Like
# run_code, this is host access — keep the bot locked to TELEGRAM_ALLOWED_USERS.
TOOLS_SHELL = os.environ.get("TOOLS_SHELL", "false").strip().lower() in ("1", "true", "yes")
TOOLS_SHELL_TIMEOUT = _int("TOOLS_SHELL_TIMEOUT", 30)
# Email tool — wraps the himalaya CLI (uses its configured accounts).
TOOLS_EMAIL = os.environ.get("TOOLS_EMAIL", "false").strip().lower() in ("1", "true", "yes")
TOOLS_EMAIL_TIMEOUT = _int("TOOLS_EMAIL_TIMEOUT", 30)
HIMALAYA_BIN = os.environ.get("HIMALAYA_BIN", "/home/ubuntu/.local/bin/himalaya").strip()

_PROTEUS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── OpenAI Codex (ChatGPT subscription via OAuth) ────────────────────────────
# Used when MODEL is prefixed `codex/…` (e.g. MODEL=codex/gpt-5.5). proteus keeps
# its OWN OAuth token chain (refresh tokens are single-use, so it must not share
# Hermes's or the codex CLI's chain). Run `bash scripts/codex_login.sh` once.
CODEX_AUTH_FILE = os.path.expanduser(
    os.environ.get("CODEX_AUTH_FILE", os.path.join(_PROTEUS_DIR, ".codex-auth.json"))
)
# Where to read OAuth creds from. "auto" tries proteus's own file, then Hermes, then
# the codex CLI. External sources (hermes/codex) are READ-ONLY — proteus never
# refreshes a chain it shares, since OAuth refresh tokens are single-use and that
# would break the owner. Hermes keeps its token fresh (multi-day validity), so
# read-only piggybacking is robust.
CODEX_AUTH_SOURCE = os.environ.get("CODEX_AUTH_SOURCE", "auto").strip()  # auto|own|hermes|codex
HERMES_AUTH_FILE = os.path.expanduser(os.environ.get("HERMES_AUTH_FILE", "~/.hermes/auth.json"))
CODEX_CLI_AUTH_FILE = os.path.expanduser(os.environ.get("CODEX_CLI_AUTH_FILE", "~/.codex/auth.json"))
CODEX_BASE_URL = os.environ.get("CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex").rstrip("/")
CODEX_CLIENT_ID = os.environ.get("CODEX_CLIENT_ID", "app_EMoamEEZ73f0CkXaXp7hrann").strip()
CODEX_ISSUER = os.environ.get("CODEX_ISSUER", "https://auth.openai.com").rstrip("/")
CODEX_TOKEN_URL = f"{CODEX_ISSUER}/oauth/token"
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip()  # "" disables

# ── Tiered memory (Postgres + pgvector) ──────────────────────────────────────
MEMORY_ENABLED = os.environ.get("MEMORY_ENABLED", "true").strip().lower() in ("1", "true", "yes")
MEMORY_RECENT_MESSAGES = _int("MEMORY_RECENT_MESSAGES", 12)   # working-memory window
MEMORY_RECALL_K = _int("MEMORY_RECALL_K", 5)                  # long-term hits injected
MEMORY_RECALL_MIN_SCORE = float(os.environ.get("MEMORY_RECALL_MIN_SCORE", "0.25") or 0.25)
MEMORY_DISTILL_EVERY = _int("MEMORY_DISTILL_EVERY", 3)        # distill every N user turns
MEMORY_DISTILL_WINDOW = _int("MEMORY_DISTILL_WINDOW", 14)     # messages examined per distill
MEMORY_DEDUP_SIM = float(os.environ.get("MEMORY_DEDUP_SIM", "0.92") or 0.92)
MEMORY_MAX_PER_USER = _int("MEMORY_MAX_PER_USER", 40)        # soft cap → curator consolidates
# The curator uses the SAME model as the main chat (MODEL) by default — leave
# MEMORY_CURATOR_MODEL empty to inherit it. Curation is fire-and-forget (never
# blocks a reply), and on codex reasoning models it runs at MEMORY_CURATOR_EFFORT
# (low) so background curation stays quick. Override the model only if you really
# want a different one.
MEMORY_CURATOR_MODEL = os.environ.get("MEMORY_CURATOR_MODEL", "").strip() or MODEL
MEMORY_CURATOR_EFFORT = os.environ.get("MEMORY_CURATOR_EFFORT", "low").strip()  # codex models only
# Persistent image memory: caption inbound images (with the vision MODEL) and store
# the description as memory text, so images are recalled/distilled like any other turn.
MEMORY_VISION_CAPTION = os.environ.get("MEMORY_VISION_CAPTION", "true").strip().lower() in ("1", "true", "yes")

# ── Cron / scheduled tasks ───────────────────────────────────────────────────
CRON_ENABLED = os.environ.get("CRON_ENABLED", "true").strip().lower() in ("1", "true", "yes")
# Run the scheduler loop inside the WEB process. Needed for an HTTP-only
# deployment: without channels there is no channels_runner, so nothing would
# ever fire the jobs the schedule tool creates. Requires WORKERS=1, or several
# workers race and a job fires more than once.
CRON_IN_WEB = os.environ.get("CRON_IN_WEB", "false").strip().lower() in ("1", "true", "yes")
CRON_TZ = os.environ.get("CRON_TZ", "UTC").strip()          # cron expressions interpreted in this tz
MAX_JOBS_PER_USER = _int("MAX_JOBS_PER_USER", 20)
CRON_CHECK_INTERVAL = _int("CRON_CHECK_INTERVAL", 30)       # scheduler tick seconds

# Start channel pollers inside the web process (only safe with WORKERS=1).
# In production run the dedicated `app.channels_runner` process instead.
RUN_CHANNELS_IN_WEB = os.environ.get("RUN_CHANNELS_IN_WEB", "").strip().lower() in ("1", "true", "yes")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_MODE = os.environ.get("TELEGRAM_MODE", "polling").strip()  # polling | webhook
TELEGRAM_ALLOWED_USERS = [
    u.strip() for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()
]

# ── WhatsApp (Meta Cloud API) ────────────────────────────────────────────────
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "").strip()
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "").strip()
WHATSAPP_GRAPH_VERSION = os.environ.get("WHATSAPP_GRAPH_VERSION", "v21.0").strip()

# ── Signal (signal-cli-rest-api) ─────────────────────────────────────────────
SIGNAL_CLI_REST_URL = os.environ.get("SIGNAL_CLI_REST_URL", "").rstrip("/")
SIGNAL_NUMBER = os.environ.get("SIGNAL_NUMBER", "").strip()
SIGNAL_POLL_INTERVAL = _int("SIGNAL_POLL_INTERVAL", 3)
