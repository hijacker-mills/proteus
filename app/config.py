"""Environment-driven configuration. Loaded once at import."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _req(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Required env var {key} is not set")
    return val


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


# Client auth
API_KEY = os.environ.get("API_KEY", "").strip()

# Server
HOST = os.environ.get("HOST", "0.0.0.0").strip()
PORT = _int("PORT", 18791)
WORKERS = _int("WORKERS", 4)

# Model (provider-prefixed, LiteLLM style: anthropic/…, openai/…, ollama/…, openrouter/…)
MODEL = os.environ.get("MODEL", "anthropic/claude-sonnet-4-6").strip()
MAX_TOKENS = _int("MAX_TOKENS", 1500)
MAX_TOOL_TURNS = _int("MAX_TOOL_TURNS", 8)
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.3") or 0.3)
# Provider API keys are read by LiteLLM directly from the environment
# (ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY, GROQ_API_KEY, …).
# python-dotenv has already loaded them; nothing else to wire up here.

# Postgres (Neon pooled endpoint)
DATABASE_URL = _req("DATABASE_URL")
DB_POOL_MIN = _int("DB_POOL_MIN", 2)
DB_POOL_MAX = _int("DB_POOL_MAX", 20)

# Embeddings
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "mxbai-embed-large").strip()

# IntelliQ app (proxy tools)
INTELLIQ_APP_URL = os.environ.get("INTELLIQ_APP_URL", "http://127.0.0.1:3001").rstrip("/")
QUBI_INTERNAL_SECRET = os.environ.get("QUBI_INTERNAL_SECRET", "").strip()
# Map channel identities → IntelliQ student ids for the IntelliQ tools.
# Format: "telegram:8707843599=cmnw...uelw,signal:+44...=cuid2"
INTELLIQ_USER_MAP = {
    k.strip(): v.strip()
    for pair in os.environ.get("INTELLIQ_USER_MAP", "").split(",") if "=" in pair
    for k, v in [pair.split("=", 1)]
}

# Optional integrations
IMAGE_SEARCH_SERVICE_URL = os.environ.get("IMAGE_SEARCH_SERVICE_URL", "").rstrip("/")
IMAGE_SEARCH_SERVICE_TOKEN = os.environ.get("IMAGE_SEARCH_SERVICE_TOKEN", "").strip()
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
TOOL_EVENT_STREAM = os.environ.get("TOOL_EVENT_STREAM", "acag:tool-events").strip()

# ── Agent identity & tools ───────────────────────────────────────────────────
# SYSTEM_PROMPT_FILE is relative to the app/ package dir.
SYSTEM_PROMPT_FILE = os.environ.get("SYSTEM_PROMPT_FILE", "prompts/assistant.md").strip()
# TOOLSET: "none" (plain chat) | "agent" (web search/fetch + code) | "intelliq" (study tools)
TOOLSET = os.environ.get("TOOLSET", "none").strip()

# ── Profiles (persona + toolset selectable per request via X-Acag-Profile) ───
# "assistant" = SYSTEM_PROMPT_FILE + TOOLSET (the general agent, used by channels).
# "qubi"      = the IntelliQ study companion, served to the IntelliQ app.
DEFAULT_PROFILE = os.environ.get("DEFAULT_PROFILE", "assistant").strip()
QUBI_PROMPT_FILE = os.environ.get("QUBI_PROMPT_FILE", "qubi_soul.md").strip()
QUBI_TOOLSET = os.environ.get("QUBI_TOOLSET", "intelliq").strip()

# ── Agent tools ──────────────────────────────────────────────────────────────
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "").strip()  # optional; else DuckDuckGo
WEB_FETCH_MAX_CHARS = _int("WEB_FETCH_MAX_CHARS", 6000)
# Code execution is OFF by default — it runs arbitrary code on this host. Only
# enable AFTER restricting who can reach the bot (e.g. TELEGRAM_ALLOWED_USERS).
TOOLS_CODE_EXEC = os.environ.get("TOOLS_CODE_EXEC", "false").strip().lower() in ("1", "true", "yes")
TOOLS_CODE_TIMEOUT = _int("TOOLS_CODE_TIMEOUT", 12)
# Browser tool — drives a real headless Chrome via the pw-control CLI (Playwright/CDP).
TOOLS_BROWSER = os.environ.get("TOOLS_BROWSER", "true").strip().lower() in ("1", "true", "yes")
PW_CONTROL_BIN = os.environ.get("PW_CONTROL_BIN", "/home/ubuntu/.nvm/versions/node/v22.22.2/bin/pw-control").strip()
PW_CONTROL_CDP_PORT = _int("PW_CONTROL_CDP_PORT", 9222)
TOOLS_BROWSER_TIMEOUT = _int("TOOLS_BROWSER_TIMEOUT", 45)
# Shell tool — runs arbitrary commands (every CLI: himalaya, aws, git, hf, …). Like
# run_code, this is host access — keep the bot locked to TELEGRAM_ALLOWED_USERS.
TOOLS_SHELL = os.environ.get("TOOLS_SHELL", "false").strip().lower() in ("1", "true", "yes")
TOOLS_SHELL_TIMEOUT = _int("TOOLS_SHELL_TIMEOUT", 30)
# Email tool — wraps the himalaya CLI (uses its configured accounts).
TOOLS_EMAIL = os.environ.get("TOOLS_EMAIL", "false").strip().lower() in ("1", "true", "yes")
TOOLS_EMAIL_TIMEOUT = _int("TOOLS_EMAIL_TIMEOUT", 30)
HIMALAYA_BIN = os.environ.get("HIMALAYA_BIN", "/home/ubuntu/.local/bin/himalaya").strip()

# ── Channel sessions (per-sender conversation memory) ────────────────────────
_ACAG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── OpenAI Codex (ChatGPT subscription via OAuth) ────────────────────────────
# Used when MODEL is prefixed `codex/…` (e.g. MODEL=codex/gpt-5.5). acag keeps
# its OWN OAuth token chain (refresh tokens are single-use, so it must not share
# Hermes's or the codex CLI's chain). Run `bash scripts/codex_login.sh` once.
CODEX_AUTH_FILE = os.path.expanduser(
    os.environ.get("CODEX_AUTH_FILE", os.path.join(_ACAG_DIR, ".codex-auth.json"))
)
# Where to read OAuth creds from. "auto" tries acag's own file, then Hermes, then
# the codex CLI. External sources (hermes/codex) are READ-ONLY — acag never
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
CRON_TZ = os.environ.get("CRON_TZ", "UTC").strip()          # cron expressions interpreted in this tz
CRON_CHECK_INTERVAL = _int("CRON_CHECK_INTERVAL", 30)       # scheduler tick seconds

SESSION_MAX_MESSAGES = _int("SESSION_MAX_MESSAGES", 20)
SESSION_TTL_SECONDS = _int("SESSION_TTL_SECONDS", 86400)
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
