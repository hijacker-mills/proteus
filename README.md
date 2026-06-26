# acag — Agentic Chat Application Gateway

A **stateless, model-agnostic** agent gateway: a provider-neutral tool-use loop
with SSE streaming, an OpenAI-compatible HTTP API, and **messaging channels**
(Telegram, WhatsApp, Signal) — built to scale horizontally.

The agent's identity and tools are pluggable (`SYSTEM_PROMPT_FILE` + `TOOLSET`).
It ships as a **general assistant** by default; the IntelliQ study agent is an
opt-in toolset (`TOOLSET=intelliq`, `SYSTEM_PROMPT_FILE=qubi_soul.md`).

## Why acag exists

Hermes is a personal-assistant runtime (messaging platforms, kanban, skills,
terminal/browser tools, session persistence). For Qubi we used ~5% of it and hit
its ceilings: a hard-coded `_MAX_CONCURRENT_RUNS = 10`, a single event loop,
sync DB drivers, and per-request auxiliary-provider probing. acag is the inverse:
one job, done statelessly, so it scales by adding replicas.

## Design

```
Mobile / Web ─▶ nginx (LB + SSE passthrough)
             ─▶ acag replicas (N× stateless; uvicorn workers, async)
                  POST /v1/chat/completions  (OpenAI-compatible)
                  · llm.py  → LiteLLM → any provider (Anthropic/OpenAI/OpenRouter/Ollama…)
                  · tool-use loop → SSE out
             ─▶ tools:
                  · memory  → asyncpg pool → Neon (-pooler endpoint)
                  · proxy   → httpx → IntelliQ /api/internal/qubi/*
             ─▶ tool events → inline in SSE  (+ optional Redis stream)
```

Two properties do the heavy lifting:

- **Stateless.** The client sends full history every turn; no session store. Any
  replica serves any request → scale = add replicas behind a load balancer.
- **Model-agnostic.** The agent loop speaks OpenAI-shaped messages/tools and
  never imports a provider SDK. `app/llm.py` is the only provider-aware file; it
  wraps LiteLLM, which talks each backend's *native* API. Change models with one
  env var:

  ```
  MODEL=anthropic/claude-sonnet-4-6      # current
  MODEL=openai/gpt-4o-mini
  MODEL=openrouter/anthropic/claude-3.5-sonnet
  MODEL=groq/llama-3.3-70b-versatile
  MODEL=ollama/llama3.1                  # fully local
  ```
  …plus the matching provider key in `.env`. To drop LiteLLM, replace `llm.py`
  alone.

## Security: server-side user scoping

`user_id` is **not** a tool parameter (unlike the Hermes plugin). The gateway
resolves it from the authenticated request and injects it at dispatch. The model
cannot see or spoof it, so it physically cannot read another student's data.

Resolution order: `X-Qubi-User-Id` header → OpenAI `user` body field →
`Student user_id: <id>` system message (back-compat).

## Channels

Messaging integrations modeled on Hermes's platform adapters. Unlike the
stateless `/v1` path, channels keep **per-sender conversation memory** in a
session store (Redis when `REDIS_URL` is set — shared across replicas + the
poller; in-memory fallback for single-process dev).

| Channel | Inbound | Enable with |
|---|---|---|
| **Telegram** | long-poll (default) or webhook | `TELEGRAM_BOT_TOKEN` (from @BotFather) |
| **WhatsApp** | webhook (Meta Cloud API) | `WHATSAPP_ACCESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` |
| **Signal** | poll (signal-cli-rest-api) | `SIGNAL_CLI_REST_URL` + `SIGNAL_NUMBER` |

Shared inbound flow (`channels/base.py → handle_inbound`): dedup by message id →
per-session lock (serialize a sender's burst) → load history → run agent → save →
reply. Built-in `/reset` command clears a conversation.

**Process model (important):** webhook channels (WhatsApp, Telegram-webhook) are
served by the web workers. **Polling channels (Telegram long-poll, Signal) must
run in exactly one process** — running them in N uvicorn workers would have them
fight over the same message stream. Use the dedicated poller:

```bash
pm2 start ecosystem.config.js                 # starts both `acag` (web) and `acag-channels` (poller)
# or just the poller:
bash scripts/run_channels.sh
# dev shortcut (single web worker, no separate process):
RUN_CHANNELS_IN_WEB=true WORKERS=1 bash scripts/run.sh
```

Hardening adopted from Hermes: inbound idempotency/dedup, per-session
serialization, long-message chunking, bounded send retry, Telegram
`deleteWebhook` before polling (avoids 409), WhatsApp `X-Hub-Signature-256`
verification. Deliberately **out of scope for v1**: interactive buttons, media,
draft/edit streaming, group/thread sessions.

## Memory (tiered, on Postgres + pgvector — not Redis)

Channels get real agent memory, not a cache. For an agent, Redis is both extra
infra *and* insufficient (no durability, no semantic recall); we already run
pgvector + an embedding pipeline, so memory lives there.

- **Working memory** — `acag_message`: durable conversation log, fetched by recency.
- **Long-term memory** — `acag_memory`: distilled facts/preferences + `vector(1024)`
  embeddings, fetched by semantic similarity (HNSW).
- **Per turn** — context = recent N messages **+** top-K semantically-recalled
  memories (injected as system context). See `memory.prepare()`.
- **After turn** — `memory.record()` logs both messages; every
  `MEMORY_DISTILL_EVERY` user-turns a background distiller extracts durable facts
  via the LLM (model-agnostic), embeds + dedups them.

Result: the bot remembers a user across sessions/restarts and recalls the
*relevant* past, not just the recent. `/reset` clears working memory (keeps
long-term). Tuning: `MEMORY_*` in `.env`. Keyed per `channel:sender`;
cross-channel identity unification is a future layer over the same `user_key`.

## Files

| Path | Role |
|---|---|
| `app/main.py` | FastAPI: `/v1/chat/completions`, `/healthz`, `/v1/models`, auth, SSE/JSON |
| `app/agent.py` | Provider-neutral tool-use loop (emits text/tool/done/error events) |
| `app/llm.py` | **Only** provider-aware module — LiteLLM wrapper, unified streaming |
| `app/toolsets.py` | Pluggable toolset selector (`none` / `intelliq`) |
| `app/schemas.py` | IntelliQ tools in OpenAI function format (no `user_id`) |
| `app/tools/*` | IntelliQ proxy + pgvector memory tools (opt-in toolset) |
| `app/channels/base.py` | Shared inbound flow: dedup → lock → memory → agent → reply |
| `app/channels/{telegram,whatsapp,signal}.py` | Channel adapters |
| `app/memory/` | Tiered memory: store (pgvector), embed, distiller, prepare/record API |
| `app/channels_runner.py` | Single-process poller for Telegram/Signal |
| `app/events.py` | Tool-event shape + optional Redis stream |
| `app/db.py` | asyncpg pool (PgBouncer-safe: `statement_cache_size=0`) |

## Run

```bash
cd /home/ubuntu/intelliQ/acag
cp .env.example .env          # fill in secrets (already populated on this box)
bash scripts/setup.sh         # create .venv + install
bash scripts/run.sh           # uvicorn, WORKERS workers on :18791
# or:  pm2 start ecosystem.config.js
bash scripts/smoke.sh         # end-to-end check
```

## Point existing clients at acag

acag is OpenAI-compatible and reuses the **same bearer token** as the Hermes
gateway, so switching is URL-only:

```
# intelliq-web/.env  and  qubi-staging/.env.local
HERMES_QUBI_GATEWAY_URL=http://127.0.0.1:18791/v1     # was :18790
# token unchanged
```

## Scale notes

- **acag:** I/O-bound; one box of `WORKERS=4` async workers handles many hundreds
  of concurrent streams. Add replicas + LB for more. No shared state.
- **Postgres:** uses the Neon **`-pooler`** endpoint; each worker keeps a small
  asyncpg pool, PgBouncer multiplexes onto the backend. Far below the 450 cap.
- **The real ceiling is the model provider's rate limit.** 2000 concurrent
  Claude conversations needs an enterprise TPM/RPM tier (or queue with
  backpressure). This gates every architecture equally — solve it at the provider
  level, not in code. Model-agnosticism helps here too: you can spread load
  across providers or fail over.
- **Embeddings:** memory tools embed via a single Ollama instance; that becomes
  its own bottleneck at scale — front it with replicas or a hosted embedder.

## Load test

```bash
.venv/bin/python tests/loadtest.py --n 100 --concurrency 50
```
