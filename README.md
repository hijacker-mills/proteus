# proteus

**Many agents. One gateway.**

A **stateless, model-agnostic agent gateway**. Define as many agents as you
like, each with its own persona and its own tools, and serve all of them from a
single process over one OpenAI-compatible endpoint, plus messaging channels.

An agent is a **file, not a process**. That is the whole point. The usual way to
run twenty agents is twenty deployments, each with its own machine, memory
footprint, idle cost and restart. Here, twenty agents is twenty Markdown files
and one gateway, because the request carries which agent it wants and the
gateway is stateless enough that any replica can serve any turn for any agent.

```
                     ┌── agents/support.md   (persona + toolset)
your app ──HTTP──▶   ├── agents/triage.md
Telegram ────────▶   ├── agents/researcher.md          one stateless process
WhatsApp ────────▶ ──┤   …add a file, not a box        serves all of them
Signal   ────────▶   └── agents/assistant.md
                                │
                                ├─▶ tool loop ─▶ your tools ─▶ SSE + tool events
                                └─▶ any model: Anthropic · OpenAI · OpenRouter
                                    Groq · Gemini · Ollama · ChatGPT-OAuth
```

Measured on one 8-core box: **1000 concurrent streams on 0.8 of a core**, so the
gateway is nowhere near the constraint. [See the numbers](#measured-performance).

**Contents:** [Quickstart](#quickstart) · [API](#api) · [Configuration](#configuration) ·
[Models](#models--providers) · [Agents](#agents) · [Tools](#toolsets) · [CLI](#the-cli) ·
[Security](#security-model) · [Channels](#channels) · [Memory](#memory) ·
[Scheduled tasks](#scheduled-tasks) · [Integrations](#building-an-integration) ·
[Deploying](#deploying) · [Performance](#measured-performance) · [Scaling](#scaling-notes)

---

## Why Proteus exists

Proteus was the sea god who took many forms while remaining one being. That is
the architecture: many agents, one gateway.

The problem it solves is the cost of the obvious alternative. Give every agent
its own deployment and you pay for every one of them separately: a machine each,
a cold start each, a config each, a restart each, and an idle bill for the
nineteen that nobody is talking to right now. Worse, the cost scales with how
many agents you *define*, not with how much they are *used*.

Proteus inverts that. Agents are definitions, the gateway is the only runtime:

- **Many agents, one process.** An agent is `(persona, toolset)` in a Markdown
  file. The request names which one it wants; the gateway loads it and answers.
  Adding an agent is adding a file, and it takes effect without a restart.
- **Stateless.** The client sends the full history each turn, so there is no
  session store on the request path. Any replica can serve any turn for any
  agent, which is why scaling is "add replicas", not "shard the sessions".
- **Model-agnostic.** The tool loop speaks OpenAI-shaped messages and never
  imports a provider SDK. `app/llm.py` is the only provider-aware file, and each
  agent may pin its own model or inherit the default.
- **One security model for all of them.** `user_id` is injected server-side, so
  no agent, and no tool anyone adds, can reach another user's data.

Non-goals: it is not a chat UI, not a RAG framework, and not an eval harness.

---

## Quickstart

Requires Python 3.11+. Postgres is optional — without it you get a stateless
chat gateway, which is all the HTTP API needs; add one (with the
[pgvector](https://github.com/pgvector/pgvector) extension) to turn on memory,
channels and scheduled jobs.

```bash
git clone https://github.com/hijacker-mills/proteus.git
cd proteus
bash scripts/setup.sh         # venv, install, and put `proteus` on PATH
cp .env.example .env          # set API_KEY, MODEL + a provider key
proteus doctor                # check the config, and say what is wrong
proteus serve                 # uvicorn on :18791
```

`setup.sh` installs editable and symlinks the CLI into `~/.local/bin`, so
`proteus` works from any directory and still resolves `agents/`, `tools/` and
`.env` from this checkout.

Then:

```bash
curl -s localhost:18791/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Proteus-User-Id: alice" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}]}'
```

`bash scripts/smoke.sh` runs an end-to-end check (health, non-streaming,
streaming). The minimum viable `.env` is four lines, with no database:

```ini
API_KEY=some-long-random-string
MODEL=anthropic/claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-ant-…
TOOLSET=none                  # plain chat; see Toolsets to add capabilities
```

Add `DATABASE_URL=postgres://user:pass@host/db` when you want memory, channels
or scheduled jobs. `/healthz` will report `degraded` until you do, which is the
expected state for a pure chat gateway rather than an error.

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | Chat. OpenAI-compatible request/response; `"stream": true` for SSE. |
| `GET /v1/models` | Advertises the configured model. |
| `GET /healthz` | Liveness: DB reachability **and** model-credential status. `ok` / `degraded` / `down` — see [Health and degraded mode](#health-and-degraded-mode). |
| `POST /channels/telegram/webhook` | Telegram webhook mode (no-op unless configured). |
| `POST /channels/whatsapp/webhook` | WhatsApp Cloud API webhook (HMAC-verified). |

### Request headers

| Header | Meaning |
|---|---|
| `Authorization: Bearer <API_KEY>` | Required unless `API_KEY` is empty (dev-only open mode). |
| `X-Proteus-User-Id` | The user every tool call is scoped to. See [Security](#security-model). |
| `X-Proteus-Profile` | Which agent (persona + toolset) to run — e.g. `assistant`, `support`. |
| `X-Proteus-Admin-Key` | Unlocks host-access tools. Off unless `ADMIN_API_KEY` is set. |
| `X-Proteus-Mode` | Selects one of the agent's named [modes](#modes), if it defines any. |

The user id may also arrive as the OpenAI `user` body field, as a
`session_key: "<profile>:<id>"` field, or as a `Student user_id: <id>` line in a
system message (back-compat). Profile likewise falls back to `body.profile` and
then to the `session_key` prefix.

### Streaming shape

Standard OpenAI SSE chunks, plus one extension: **completed tool calls are
streamed inline** on a `proteus_tool_event` key, so a client can render "searching
the web…" or a result card live without a second channel.

```jsonc
data: {"id":"chatcmpl-…","choices":[{"delta":{"content":"Photo"},"index":0}]}
data: {"id":"chatcmpl-…","choices":[{"delta":{},"index":0}],
       "proteus_tool_event":{"ts":1787004212975,"userId":"alice","tool":"web_search",
                          "status":"ok","ms":412,"query":"…","hits":5}}
data: [DONE]
```

Non-streaming responses collect the same events into a `proteus_tool_events` array.
A tool may also attach a small render payload on `event.data`, so a UI can draw
a card from the real result instead of re-parsing it out of the model's prose.
Set `REDIS_URL` to also mirror every event to a Redis stream for audit and
analytics.

---

## Configuration

Everything is environment-driven (`.env` is loaded at import). Full list with
comments in [`.env.example`](.env.example); the essentials:

**Core**

| Var | Default | Notes |
|---|---|---|
| `API_KEY` | *(empty)* | Bearer token callers must present. Empty = open, dev only. |
| `ADMIN_API_KEY` | *(empty)* | Second key gating host tools over HTTP. Leave empty. |
| `HOST` / `PORT` / `WORKERS` | `0.0.0.0` / `18791` / `4` | uvicorn binding + worker count. |
| `DATABASE_URL` | *(empty)* | Postgres. Optional — unset or unreachable runs stateless. Use a pooled endpoint if you have one. |
| `REDIS_URL` | *(empty)* | Optional: cross-replica dedup + tool-event stream. |

**Model**

| Var | Default | Notes |
|---|---|---|
| `MODEL` | `anthropic/claude-sonnet-4-6` | Provider-prefixed, LiteLLM style. `mock/*` for [load tests](#load-testing). |
| `MAX_TOKENS` / `TEMPERATURE` | `1500` / `0.3` | Ignored by providers that don't accept them. |
| `REQUEST_TIMEOUT` / `LLM_RETRIES` | `300` / `2` | Ceiling on one completion; retries for transient 429/5xx. |
| `MAX_CONCURRENT_COMPLETIONS` | `64` | In-flight completions **per worker**, so the real cap is this × `WORKERS`. Over it, `503` + `Retry-After`. `0` disables. |
| `CONCURRENCY_WAIT` | `20` | Seconds a request waits for a slot before being shed. |
| `MAX_PARALLEL_TOOLS` | `8` | Tool calls run concurrently within one turn, up to this many. |
| `PROMPT_CACHING` | `true` | Explicit cache breakpoint for providers that need one (Anthropic). Others cache automatically. |
| `STREAM_COALESCE_MS` | `0` | Batch SSE deltas into one frame for N ms. `0` = a frame per token. Never delays the first token. |
| `MAX_TOOL_TURNS` | `8` | Tool-loop iterations before Proteus stops and says so. |

**Agent identity + tools**

| Var | Default | Notes |
|---|---|---|
| `SYSTEM_PROMPT_FILE` | `prompts/assistant.md` | Persona for the `assistant` profile, relative to `app/`. |
| `TOOLSET` | `none` | Fallback toolset when there is no `agents/` dir. Comma list: `none` / `basics` / `web` / `agent` / `custom`. |
| `DEFAULT_PROFILE` | `assistant` | Used when a request names no profile. |
| `TOOLS_BROWSER` | `true` | Real headless-Chrome tool (via the `pw-control` CLI). |
| `TOOLS_CODE_EXEC` / `TOOLS_SHELL` / `TOOLS_EMAIL` | `false` | Host-access tools — read [Security](#security-model) first. |
| `TAVILY_API_KEY` | *(empty)* | Better web search; falls back to keyless DuckDuckGo. |

**Memory / embeddings**

| Var | Default | Notes |
|---|---|---|
| `MEMORY_ENABLED` | `true` | Tiered memory for channel conversations. |
| `OLLAMA_URL` / `EMBEDDING_MODEL` | `http://127.0.0.1:11434` / `mxbai-embed-large` | 1024-dim embeddings. |
| `MEMORY_RECENT_MESSAGES` / `MEMORY_RECALL_K` | `12` / `5` | Working-memory window / recalled facts per turn. |
| `MEMORY_DISTILL_EVERY` / `MEMORY_MAX_PER_USER` | `3` / `40` | Curation cadence / soft cap. |

Channel (`TELEGRAM_*`, `WHATSAPP_*`, `SIGNAL_*`), cron (`CRON_*`) and Codex
OAuth (`CODEX_*`) variables are documented in their own sections below.

---

## Models & providers

`app/llm.py` wraps [LiteLLM](https://github.com/BerriAI/litellm), which speaks
each provider's native API while exposing one streaming + tool-calling interface.
Change models with one variable (plus the matching key):

```ini
MODEL=anthropic/claude-sonnet-4-6
MODEL=openai/gpt-4o-mini
MODEL=openrouter/anthropic/claude-3.5-sonnet
MODEL=groq/llama-3.3-70b-versatile
MODEL=ollama/llama3.1                    # fully local
MODEL=codex/gpt-5.5                      # ChatGPT subscription via OAuth (below)
```

> **Caveat:** routing is model-agnostic, tool-calling *fidelity* is not. Small
> local models often emit tool calls as plain-text JSON instead of structured
> `tool_calls`, so nothing dispatches. Use a frontier model for tool-using work.

To drop LiteLLM entirely, reimplement `astream()` in `llm.py` — the agent loop
depends only on its normalized `{"type":"text"|"final"}` event contract.

### ChatGPT subscription (Codex OAuth)

`MODEL=codex/…` routes to the ChatGPT Responses backend using OAuth credentials
instead of a paid API key (`app/codex_auth.py` + `app/codex_provider.py`). Log in
once with `bash scripts/codex_login.sh` (device-code flow) — Proteus stores its own
token chain in `.codex-auth.json` and refreshes it automatically.

`CODEX_AUTH_SOURCE=auto` will also *read* credentials from another local tool's
auth file if Proteus has none of its own, but it will never refresh a chain it
doesn't own: OAuth refresh tokens are single-use, so refreshing a shared chain
would break the owner. Borrowed credentials therefore expire on someone else's
schedule — `/healthz` reports `model_auth.expires_at` and starts warning 48h out.

---

## Agents

An agent is `(persona, toolset)`. One Markdown file each, in `agents/`, and one
gateway serves all of them. Frontmatter carries the config, the body **is** the
system prompt, because the prompt is the part you actually edit and prose does
not belong inside YAML:

```markdown
---
name: support
description: Answers product questions and files tickets
toolset: [web, custom]
model: null          # null or omitted = inherit MODEL
max_tokens: 1500
---

You are a support agent for Acme. Answer from the documentation you can search,
say plainly when you do not know, and never invent a policy…
```

```bash
proteus agent new researcher --toolset "web,custom"
proteus agent list
proteus agent show support --prompt
```

```bash
curl … -H "X-Proteus-Profile: support" -d '{"messages":[…]}'
```

Selection order: `X-Proteus-Profile` → `body.profile` → the `session_key` prefix
→ `DEFAULT_PROFILE`. Files are re-read when they change, so adding or editing an
agent needs no restart. The header is still called *profile* rather than *agent*
because renaming it would break existing clients for no functional gain.

### Modes

An agent may declare named behaviour blocks. A request selects one with
`X-Proteus-Mode` (or `body.mode`), and it is appended to that agent's system
prompt for that request only — no wire-format or tool-loop change:

```markdown
---
name: support
toolset: [web]
modes:
  terse: |
    Answer in at most three sentences. No preamble.
  detailed: |
    Give the full explanation, with a worked example and the edge cases.
---
```

The gateway supplies the mechanism; the agent supplies the content. An absent or
unknown mode injects nothing, so callers that never send one are unaffected.

**No `agents/` directory?** One agent is assembled from `SYSTEM_PROMPT_FILE` +
`TOOLSET` and named after `DEFAULT_PROFILE`, so the gateway serves rather than
refusing to start.

**Storage is a seam.** `agents_store.AgentStore` has three methods, and files
are one implementation. Keeping agents in files keeps them in git, reviewable
and revertible, and keeps chat free of a database dependency. When several
replicas need a single source of truth, a Postgres-backed store slots in behind
the same interface without touching any caller.

Client-supplied `system` messages are folded into the agent's system prompt
rather than dropped, so a caller can layer per-surface instructions (formatting
rules, a user profile blurb) without forking the persona. Treat that as trusted
input: it comes from your server, not the end user.

---

## Toolsets

`TOOLSET` is a comma-separated list; Proteus merges the schemas and routes each call
to the toolset that owns it (`app/toolsets.py`).

| Toolset | Tools |
|---|---|
| `none` | — plain conversational agent |
| `basics` | `datetime_now`, `calculate`, `remember`, `recall`, `todo` — things models get wrong unaided |
| `files` | `read_file`, `list_files` — confined to `FILES_ROOT`, so not host access |
| `web` | `web_search`, `web_fetch`, `browser` — safe research subset, no host access |
| `agent` | the `web` tools **+** `run_code`, `shell`, `email`, `schedule` (each individually gated) |
| `custom` | your own `tools/*.md` and `app/tools/custom/*.py` — see [Tools as files](#tools-as-files) |

| Tool | What it does |
|---|---|
| `datetime_now` | The date/time in any IANA timezone, optionally shifted. Models do not know what day it is. |
| `calculate` | Exact arithmetic over a whitelisted AST — not `eval`, so it is not a host tool. |
| `remember` / `recall` | Durable per-user notes in proteus's own table, scoped to the authenticated user. |
| `todo` | A task list the model keeps for itself across a multi-step job. Persisted per user, so it survives the next turn landing on a different worker. |
| `web_search` | Tavily / Brave / Serper if keyed, else the real browser. See [search](#search). |
| `web_fetch` | Readable page text (r.jina.ai reader, raw-strip fallback). |
| `browser` | A real headless Chromium in-process via Playwright: navigate/read/click/fill/type/press/back/eval, each returning a page snapshot. |
| `run_code` | Runs a short Python snippet in a temp dir with a timeout. |
| `shell` | Runs a host command via `bash -lc` — i.e. every CLI on the box. |
| `email` | Reads/sends mail through the `himalaya` CLI. |
| `read_file` / `list_files` | Read text files under `FILES_ROOT`. Paths resolve to a real path before the containment check, so `..`, absolute paths and symlinks out of the root are all refused. Unset root = both refuse. |
| `schedule` | Creates/lists/cancels [scheduled tasks](#scheduled-tasks). |

### Search

There is no reliable keyless web search any more: the endpoints that used to be
scrapeable now answer an HTTP client with a challenge page. So `web_search`
tries, in order:

1. **Tavily**, **Brave** or **Serper**, if you set `TAVILY_API_KEY`,
   `BRAVE_SEARCH_API_KEY` or `SERPER_API_KEY`. Use one of these in production.
2. **The real browser.** A headless Chromium with a normal user agent gets the
   real results page, and the extractor pulls title, URL and snippet out of the
   DOM. Slower and scrappier than an API, but it needs no key and it works.
3. Otherwise, a clear error naming exactly what to configure.

`SEARCH_URL_TEMPLATE` chooses the engine for the browser path.

### Tools as files

Two kinds are discovered from disk, so adding a tool needs neither a code change
nor a restart.

**Declarative HTTP** (`tools/<name>.md`) covers the common case, calling an
endpoint, with no Python at all. Same convention as agents, and for the same
reason: the body is the `description` the *model* reads, and how well it is
written decides whether the tool ever gets called.

```markdown
---
name: weather
method: GET
url: https://api.example.com/weather
auth: bearer ${WEATHER_API_KEY}     # ${VAR} is read from the environment
query:
  city: "{{city}}"                  # {{name}} is filled from the model's arguments
params:
  city: {type: string, required: true, description: City name}
---

Current weather for a city. Use when the user asks about conditions today.
```

**Python** (`app/tools/custom/<name>.py`) for anything with real logic: export
`SCHEMA` and an `async handler(user_id, args) -> dict`.

Expose either by adding `custom` to an agent's toolset. Scaffold and try them
with `proteus tool new <name> --http|--python` and `proteus tool test <name>`.

File-defined tools inherit the gateway's security model rather than sitting
outside it:

- `user_id` can never be a declared parameter. Identity comes from the
  authenticated request, so a tool cannot be aimed at another user's data.
- Placeholders are rejected in the scheme and host at load time, and allowed
  only in the path, query and body. Otherwise `url: https://{{host}}/` would
  hand the model an SSRF primitive against your internal network.
- They are never host tools, so no file can gain shell-equivalent access. A
  `tools/shell.md` does not become the real `shell`.

---

## The CLI

`proteus` manages agents and tools, and inspects a running gateway. Commands
that touch a live instance take `--remote URL`, so the same CLI drives
production.

```bash
proteus doctor                       # config, agents, services, gateway
proteus tui                          # full-screen console for trying agents out
proteus chat support                 # one conversation, streaming, in the terminal

proteus agent list                   # name, toolset, tool count, source
proteus agent new support -t web     # writes agents/support.md
proteus agent show support --prompt
proteus agent validate               # lint every agent; non-zero if any is broken

proteus tool list                    # file-defined tools + every toolset
proteus tool new weather --http      # or --python
proteus tool test calculate -a '{"expression":"2+2"}'   # any tool, not just custom

proteus serve                        # run the gateway
proteus bench -c 200 --stream        # load test (point MODEL at mock/* first)
proteus health --remote https://gw.example.com
```

Every listing command takes `--json`, written raw to stdout with diagnostics on
stderr, so it pipes into `jq` cleanly. Everything exits non-zero on failure, so
it scripts.

`proteus tui` is the one to reach for while building an agent: agents in a
sidebar, streaming replies, and each tool call shown with its status and
duration as it happens.

`proteus doctor` is the one to run after any config change. It flags an empty
`API_KEY`, a `DEFAULT_PROFILE` naming no agent, a mock model left on in
production, agents with empty prompts, dead model credentials, `CRON_IN_WEB`
with several workers, and `TOOLS_BROWSER` without Playwright — then probes
Postgres, Redis and Ollama so you find out here rather than on the first
request.

---

### Adding your own toolset

1. Write handlers with the signature `async (user_id, args) -> dict`.
2. Describe them in OpenAI function-schema format. **Omit the user id** — Proteus
   injects it (see below).
3. Register the `(schemas, dispatch)` pair in `toolsets._provider()`.
4. Set `TOOLSET=yourtoolset` (or add it to a profile).

A tool that raises never kills the run: the exception is returned to the model as
`{"error": …}` and surfaced as a tool event with `status: "error"`.

---

## Security model

### Server-side user scoping

`user_id` is **not** a tool parameter. The gateway resolves it from the
authenticated request and injects it at dispatch time, so the model can neither
see nor spoof it — cross-user isolation is enforced by the gateway, not by model
compliance. This is why tool schemas in `app/schemas.py` have no user field.

### Host tools are a separate privilege

`shell`, `run_code`, `email` and `schedule` act on the **host**, not on the
conversation (`toolsets.HOST_TOOLS`). Authenticating with `API_KEY` does not
grant them, and neither does selecting a profile — they are added only for
callers marked trusted:

| Caller | Host tools? |
|---|---|
| HTTP with `API_KEY` | **no** |
| HTTP with `API_KEY` **and** `X-Proteus-Admin-Key: $ADMIN_API_KEY` | yes — and `ADMIN_API_KEY` is unset by default, so: never |
| Channel message from a sender on that channel's allowlist | yes |
| Channel with no allowlist configured (open bot) | **no** |
| A scheduled job | re-checked at run time against its owner's allowlist |

They are withheld from the tool schemas *and* refused at dispatch, so a model
that invents the tool name gets nothing either. The reasoning: `API_KEY` is
typically shared across every product surface that talks to the gateway, so
treating it as an operator credential would make host RCE reachable by anyone who
obtains it.

### Deployment checklist

- Set `API_KEY` to something long and random. Rotate it if it ever leaks.
- Leave `ADMIN_API_KEY`, `TOOLS_SHELL`, `TOOLS_CODE_EXEC` and `TOOLS_EMAIL` off
  unless you need them, and set `TELEGRAM_ALLOWED_USERS` before you do.
- Proteus binds `0.0.0.0` by default. Put it behind a reverse proxy or bind
  `127.0.0.1` — don't rely on a cloud security group alone.
- `web_fetch` and `browser` run every URL through the **SSRF guard**
  (`app/tools/url_safety.py`), which fails closed and refuses private,
  loopback, CGNAT and cloud-metadata addresses. Metadata endpoints such as
  `169.254.169.254` stay blocked even with `ALLOW_PRIVATE_URLS=true`, since
  nothing legitimate reaches them from an agent. Set that flag only when a tool
  genuinely has to call an internal service.
- Errors are streamed to the client verbatim (`⚠️ <exception>`); wrap or redact
  them if your surface is public.

---

## Channels

Messaging integrations sharing one inbound flow (`channels/base.py`):
**dedup by message id → per-sender lock → load memory → run agent → persist →
reply**. A built-in `/reset` clears the conversation (long-term memory survives).

| Channel | Inbound | Enable with |
|---|---|---|
| **Telegram** | long-poll (default) or webhook | `TELEGRAM_BOT_TOKEN` |
| **WhatsApp** | webhook (Meta Cloud API) | `WHATSAPP_ACCESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` |
| **Signal** | poll (signal-cli-rest-api) | `SIGNAL_CLI_REST_URL` + `SIGNAL_NUMBER` |

Extras: Telegram **streams live** by editing one message as text accumulates
(with a transient `🔧 tool…` indicator); **images** on any channel are downloaded,
converted to data URLs and passed to the model as multimodal content, then
captioned in parallel so the description enters long-term memory as text.

`TELEGRAM_ALLOWED_USERS` is both an access control and the trust boundary for
host tools. An open bot gets no host tools, by design.

**Process model (important):** webhook channels are served by the web workers.
**Polling channels must run in exactly one process** — N uvicorn workers would
fight over the same update stream. Use the dedicated poller:

```bash
pm2 start ecosystem.config.js                     # web (`proteus`) + poller (`proteus-channels`)
bash scripts/run_channels.sh                      # poller only
RUN_CHANNELS_IN_WEB=true WORKERS=1 bash scripts/run.sh   # dev shortcut
```

---

## Memory

The `/v1` path is stateless by design — the client owns the history, which is
why the gateway runs without a database at all. **Channels** (and scheduled
jobs) have no client to own it, so they get real tiered memory instead, on
Postgres + pgvector:

- **Working memory** — `proteus.proteus_message`: a durable conversation log, by recency.
- **Long-term memory** — `proteus.proteus_memory`: distilled facts + `vector(1024)`
  embeddings, retrieved by semantic similarity (HNSW).
- **Per turn** — context = recent N messages **+** top-K recalled memories,
  injected as system context (`memory.prepare()`).
- **After the turn** — both messages are logged; every `MEMORY_DISTILL_EVERY`
  user-turns a background **curator** reconciles long-term memory against the
  conversation, emitting `add` / `update` / `remove` operations. It overwrites
  facts in place when they change, removes contradicted ones, dedups by
  fingerprint and cosine similarity, and stays under `MEMORY_MAX_PER_USER`.
  Curation is fire-and-forget and never blocks a reply.

Redis is deliberately *not* the memory store: for an agent it is both extra infra
and insufficient (no durability, no semantic recall). It stays optional, for
cross-replica dedup and the tool-event stream.

Memory is keyed per `channel:sender`; cross-channel identity unification would be
a layer over the same `user_key`.

> **Sharing a database with an ORM-managed app?** Keep Proteus's tables in their own
> schema — they are created in `proteus`, not `public`, and every statement in
> `memory/store.py` and `cron.py` is schema-qualified. An ORM that owns `public`
> (Prisma, in our case) treats unknown tables there as drift and will happily
> emit `DROP TABLE` into a generated migration. That happened to us twice and
> silently wiped all memory and scheduled jobs; a separate schema is invisible to
> the ORM's diff and ends the problem.

---

## Scheduled tasks

The agent can schedule its own future work (`app/cron.py`, `schedule` tool). Jobs
live in `proteus.proteus_cron` and run their natural-language prompt through the full
agent — tools and memory included — delivering the result back to the channel
they were created from.

- Recurring: a cron expression, interpreted in `CRON_TZ`.
- One-off: `in_seconds`, deleted after firing.
- **Where the result goes.** A scheduled run has nobody waiting for it. From a
  channel it replies to that chat. Over HTTP there is no push channel, so the
  caller supplies `webhook_url` and the result is POSTed there. That URL is
  SSRF-checked when the job *fires*, not only when it is created, because DNS
  can be repointed in between.
- **Something must run the loop.** With channels that is `channels_runner`. An
  HTTP-only deployment has no such process, so set `CRON_IN_WEB=true` — and
  `WORKERS=1`, or every worker runs its own scheduler and each job fires N times.
  The gateway logs a warning if you set one without the other.
- `MAX_JOBS_PER_USER` (default 20) caps enabled jobs. Each is a model call on a
  timer, so an unbounded list is a way to spend your provider quota.
- The scheduler loop runs **only** in the single `channels_runner` process (so a
  job fires once) while any process may create jobs. Due jobs are advanced or
  deleted *before* they run, so a slow job can't double-fire.
- The current UTC time is injected into the system prompt, so the model schedules
  against reality rather than its training cutoff.

---

## Building an integration

A real integration is a set of tools plus an agent that knows how to use them,
and neither needs to live in this repo. The pattern:

1. **Tools.** Anything that is an HTTP call is a `tools/*.md` file, with
   `send_user_header` passing the gateway-resolved user id to your backend so it
   can scope the response. Anything with real logic is a Python module in
   `app/tools/custom/`. Both load automatically under the `custom` toolset.
2. **An agent.** One `agents/<name>.md` naming that toolset, with its persona in
   the body and any [modes](#modes) in the frontmatter.
3. **Point at them.** `AGENTS_DIR` and `TOOLS_DIR` can live anywhere on disk, so
   your integration can be its own repo, mounted or checked out beside this one.

Nothing about your domain ends up in the gateway. The one integration this was
extracted from (a study companion, nine tools against a host app) is now exactly
this: files in another repo, and the gateway does not know it exists.

## Deploying

```bash
pm2 start ecosystem.config.js   # `proteus` (web, uvicorn multi-worker) + `proteus-channels` (poller)
```

`ecosystem.config.js` is a working reference: the web app is supervised as one
entry (uvicorn forks its own workers) and the poller is pinned to a single
instance. `kill_timeout` is raised so in-flight SSE streams drain on restart.

Behind nginx, disable buffering for SSE (`proxy_buffering off;` and pass through
`X-Accel-Buffering: no`, which Proteus already sets).

### Health and degraded mode

`GET /healthz` returns DB status, the active model, its credential expiry,
whether host tools are reachable over HTTP, and the enabled channels. `status`
is one of three values, and only one of them should pull an instance out of a
load balancer:

| `status` | HTTP | Meaning |
|---|---|---|
| `ok` | 200 | Everything reachable. |
| `degraded` | 200 | **Still serving chat**, without the database — no memory, channels or scheduled jobs. |
| `down` | 503 | Model credentials are dead, so every completion would fail. Take it out of rotation. |

The distinction matters because `/v1/chat/completions` is stateless: the
transcript arrives in the request body, so chat, tools and streaming need no
Postgres at all. Proteus therefore treats the database as optional — it boots
without one, logs a warning, retries every 60s in the background, and enables
memory, channels and cron the moment the connection opens. DB-backed tools
(`memory_*`, `schedule`) return a plain "unavailable" message meanwhile, which
the model relays to the user.

This is a deliberate blast-radius choice: a database outage costs you memory,
not the whole gateway.

---

## Measured performance

Numbers from an 8-core box, `WORKERS=4`, against the [synthetic backend](#load-testing)
so they reflect Proteus rather than a provider's speed or rate limit.

| Test | Result |
|---|---|
| 1000 concurrent live SSE streams, 400 tokens each | 1000/1000 completed, **zero errors**, every stream delivered exactly 400/400 tokens |
| Time to first token, under that full 1000-stream load | p50 **0.9s**, p95 **1.05s** |
| CPU to serve those 1000 concurrent streams | **0.8 of one core** (the load generator needed 3.7 cores to *produce* the load) |
| Memory per open stream | **~44 KB**, RSS flat across runs, no leak |
| 10,500 requests up to 2000 concurrent | **zero** failures, drops or resets |
| Server CPU per request | **1.5ms** non-streaming, **2.25ms** streaming (**1.85ms** with coalescing), so ~670 and ~440–540 completions/sec/core |
| Backpressure, 120 requests against 20 slots | 20 served, 100 clean `503` + `Retry-After`, nothing hung |
| Parallel tool calls, 5 tools × 0.4s | **0.46s**, i.e. the slowest tool, not the sum (4.4× faster than sequential) |

The headline: **1000 concurrent streams cost under one core**, and per-stream
memory is small enough that RAM is not the binding constraint either. On this
box the gateway's own ceiling is in the thousands of concurrent streams.

### Per-turn latency

Capacity is one thing; the latency of a single turn is another, and it is
dominated by work the gateway controls rather than by the transport.

- **Tool calls in a turn run concurrently** (`MAX_PARALLEL_TOOLS`, default 8).
  Models are asked for parallel tool calls and routinely emit several, so
  running them in sequence made a turn cost the *sum* of its tools. Results are
  still handed back in call order, because providers match `tool_call_id`
  positionally and reject a shuffled list.
- **The prompt is laid out for caching.** Everything stable (persona, folded
  client system messages, mode block) comes first and the clock goes last, so
  providers can cache the prefix. This matters more than it sounds: a typical
  agent carries roughly 700 tokens of persona and 1,500 of tool schemas, and all
  of it would otherwise be re-processed on every turn *and* on every tool
  round-trip inside that turn. Anthropic gets an explicit `cache_control` breakpoint placed
  just above the clock (`PROMPT_CACHING`, on by default); OpenAI and the Codex
  backend cache a stable prefix automatically and need nothing.
- **`orjson` for SSE frames**, with a stdlib fallback so it stays a soft
  dependency.
- **`STREAM_COALESCE_MS`** (default `0`, off) batches deltas into fewer frames.
  At 25ms a 120-token answer went from 122 frames to 27 with byte-identical
  text, and streaming CPU fell a further 18%. The first token is never
  buffered, so time-to-first-token is unaffected. Note the gain shrinks with a
  real model, which emits tokens far slower than the synthetic backend and so
  batches fewer per frame.

Which is the point. Once the gateway is stateless and async, your ceiling stops
being "how many agent processes can I afford to run" and becomes the provider's
tokens-per-minute. Proteus is designed to make the first problem disappear so
you only have to solve the second.

Two caveats on reading these. They use a synthetic model, so they measure
transport, the agent loop, fan-out and tool dispatch, not model latency; a real
answer adds the provider's own time to every figure. And a single-process load
generator saturates its core long before the gateway does, which is worth
knowing because it silently caps a naive test: our first run appeared to plateau
at 52 rps until we checked and found the *client* pinned at 92% CPU while the
four workers idled at under 10% each. Fork the generator, or you will measure it
instead of the server.

### Load testing

`MODEL=mock/<profile>` streams synthetic tokens with no provider involved, so a
stress test measures the gateway and costs nothing:

```ini
MODEL=mock/instant   # no delay          — pure gateway overhead
MODEL=mock/fast      # 120 tok @  5ms    — a fast hosted model
MODEL=mock/slow      # 400 tok @ 25ms    — long-lived streams
MODEL=mock/tool      # exercises the full tool round-trip
MODEL=mock/fast?tokens=800&delay_ms=10   # any shape you want
```

`/healthz` reports the active model, so a mock left on in production is visible
rather than silent.

---

## Scaling notes

- **Proteus** is I/O-bound. Measured above: 1000 concurrent streams on under one
  core, so `WORKERS=4` on one box handles thousands. Add replicas + a load
  balancer for more, since there's no shared state on the request path.
- **Set `MAX_CONCURRENT_COMPLETIONS`** to roughly what your provider tier can
  actually absorb, remembering the real cap is that value × `WORKERS`. It is the
  difference between a spike becoming "some users wait" and a spike becoming
  429s for everyone, including users already mid-answer. Watch `concurrency` on
  `/healthz`: `in_use` near `limit` means add capacity, and a climbing
  `rejected` means you needed to already.
- **Postgres:** if your provider offers a pooled endpoint, use it. Each worker
  keeps its own asyncpg pool; `statement_cache_size=0` is mandatory under
  PgBouncer transaction pooling (already set in `db.py`) or queries fail randomly
  under load. A plain local Postgres needs neither, and `db.py` skips TLS for
  loopback hosts and `sslmode=disable`.
- **Serverless Postgres bills idle time, so never poll it on a short timer.**
  Providers that autosuspend (Neon, Aurora Serverless) charge a minimum idle tail
  per query — commonly ~5 minutes. Any timer shorter than that tail pins the
  compute at a 100% duty cycle, so a 30s poll costs the same as running full time.
  The scheduler used to do exactly this and burned a month's compute allowance in
  about 16 days on queries that almost always returned zero rows. `cron.py` now
  caches when the next job is due and only queries when one actually is, using a
  Redis counter (`proteus:cron:version`) that `create_job`/`delete_job` bump so
  other processes still see new jobs promptly. Idle cost went from ~2,880
  queries/day to ~4. Apply the same rule to any timer you add.
- **Multi-replica:** set `REDIS_URL` so inbound dedup is shared. Without it,
  dedup is per-process — fine for one poller, wrong for webhook channels across
  several workers.
- **The real ceiling is the model provider's rate limit.** Thousands of
  concurrent conversations need an enterprise TPM/RPM tier, or a queue with
  backpressure. That gates every architecture equally — solve it at the provider
  level. Model-agnosticism helps: spread load across providers, or fail over.
- **Embeddings:** a single Ollama instance is its own bottleneck at scale —
  front it with replicas or use a hosted embedder.

---

## Repo layout

| Path | Role |
|---|---|
| `app/main.py` | FastAPI surface: chat, health, models, auth, SSE/JSON |
| `app/agent.py` | Provider-neutral tool-use loop (text / tool / done / error events) |
| `app/llm.py` | **Only** provider-aware module — LiteLLM wrapper |
| `app/codex_auth.py`, `app/codex_provider.py` | ChatGPT-OAuth backend |
| `app/agents_store.py`, `app/profiles.py` | Agent definitions (agents/*.md) and resolution |
| `app/toolsets.py`, `app/tools/declarative.py` | Toolset composition, privilege filtering, file-defined tools |
| `app/cli/` | The `proteus` command line |
| `app/schemas.py`, `app/tools/*` | Tool schemas and handlers |
| `app/channels/base.py` | Shared inbound flow: dedup → lock → memory → agent → reply |
| `app/channels/{telegram,whatsapp,signal}.py` | Channel adapters |
| `app/memory/` | Tiered memory: store (pgvector), embed, curator, prepare/record API |
| `app/cron.py` | Scheduled tasks + scheduler loop |
| `app/channels_runner.py` | Single-process poller for Telegram/Signal |
| `app/events.py` | Tool-event shape + optional Redis stream |
| `app/db.py` | asyncpg pool (PgBouncer-safe, optional, self-reconnecting) |

## Tests

```bash
bash scripts/smoke.sh                    # against a running instance: health, chat, streaming

set -a; . ./.env; set +a                 # the Python tests read config from the environment
.venv/bin/python tests/phase1_verify.py  # tool concurrency, ordering, prompt-cache layout (no server)
.venv/bin/python tests/tool_parallel.py  # parallel tools finish out of order, arrive in call order
.venv/bin/python tests/channel_smoke.py  # webhook verify + inbound flow + working memory
.venv/bin/python tests/memory_smoke.py   # log → curate → recall after a working-memory wipe
.venv/bin/python tests/loadtest.py --n 100 --concurrency 50   # concurrency + latency percentiles
```

`tests/phase1_verify.py` needs no server and no provider, so it is the one to
run on every change. `tests/phase1_live.py` covers what only breaks over a real
socket (unicode on the wire, tool/text interleaving, slot release on client
disconnect) and needs a running instance:

```bash
# WORKERS=1 matters: the concurrency limiter is per-process, so with several
# workers /healthz may answer from a different one and the leak check proves
# nothing. The model must be slow enough that streams are actually in flight.
MODEL="mock/tool?tokens=400&delay_ms=25" WORKERS=1 bash scripts/run.sh
.venv/bin/python tests/phase1_live.py
```

`tests/real_model.py` runs against a real provider and real tools. It is the
only test that can prove things a mock cannot: that models genuinely batch tool
calls, that the batch is dispatched concurrently, and what streaming actually
costs.

```bash
MODEL=anthropic/claude-sonnet-5 ANTHROPIC_API_KEY=sk-ant-… bash scripts/run.sh
.venv/bin/python tests/real_model.py
```

It groups tool events **by turn** before timing them, which matters: the agent
loop takes several round-trips when a model retries a failed tool, and pooling
tools from different turns measures the model's thinking rather than dispatch.
Serial dispatch cannot produce an arrival spread below
`sum(tool_ms) - slowest_tool_ms`, so that is the assertion.

The rest are live-integration scripts, not unit tests: they hit the real
database and the configured model. There is no CI suite yet.

### Prompt caching, measured

Verified on `anthropic/claude-sonnet-5` with a real agent whose stable prefix is
~2,200 tokens (persona plus 12 tool schemas):

| | cached tokens re-read per turn |
|---|---|
| system as a plain string (no breakpoint) | **0** |
| stable-first layout + `cache_control` above the clock | **3,781** |

It holds across a clock change, which is the point of moving the timestamp last.

**Minimum cacheable prefix is model-dependent and easy to trip over.** Measured
on Haiku 4.5: not cached at 3,915 tokens, cached at 4,965 — the documented floor
is 4,096, versus 1,024 for Sonnet and Opus. So a ~2,200-token agent caches on
Sonnet and silently does not on Haiku. The breakpoint is harmless either way, but
do not assume a win without checking `cache_read_input_tokens` for your own
model and prompt size.

## License

[Apache License 2.0](LICENSE) — permissive, with an explicit patent grant from
contributors. Copyright 2026 John Sackey. See [NOTICE](NOTICE).
