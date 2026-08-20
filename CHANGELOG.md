# Changelog

## Unreleased

### Added
- **Integration packs.** `PACKS=/srv/thing` mounts a directory of `agents/`,
  `tools/`, `tools/custom/` and its own `.env`, so an integration is its own
  repo rather than a fork of this one. Definitions from this deployment are
  searched first and a name clash is logged, so a pack can't silently replace an
  agent you already run.
- **Tools can claim a toolset.** `toolset: <name>` in a tool file (`TOOLSET` in
  a Python one) puts it in a toolset agents ask for by name, instead of the
  shared `custom` bucket every agent using custom tools receives.
- **A Python tool file can define several tools** via `TOOLS = [(schema,
  handler), …]`, and can import its `_`-prefixed neighbours as helpers.
- Declarative tools take an optional per-tool `timeout:`, and expand `${VAR}` in
  `url` so a backend's address is configuration rather than a committed literal.

### Fixed
- An optional declarative-tool parameter the model didn't supply was sent as an
  empty string, overriding the backend's own default — an omitted `limit`
  arrived as a limit of 1. The key is now dropped, and a supplied argument keeps
  its type instead of being stringified.

## 0.2.0

Renamed from ACAG. Many agents, one gateway.

### Added
- **Agents as files.** `agents/*.md`, frontmatter for config and the body as the
  system prompt, hot-reloaded. Named modes live in the agent, not the gateway.
- **Tools as files.** `tools/*.md` for HTTP tools with no code, and
  `app/tools/custom/*.py` for Python handlers, both auto-discovered.
- **`proteus` CLI** — agents, tools, auth, jobs, memory, config, doctor, chat,
  bench, serve. `--json` everywhere for scripting.
- **`proteus tui`** — a full-screen console for trying agents out.
- **Generic tools**: `datetime_now`, `calculate`, `remember`/`recall`, `todo`,
  `read_file`, `list_files`.
- **Usage accounting** on both response shapes, in logs and in `/metrics`,
  accumulated across a turn and including cached tokens.
- **Per-user rate limiting**, **named API keys**, and a **Prometheus endpoint**.
- **CI** on a clean runner across 3.11–3.13, plus the `setup.sh` install path.
- Apache-2.0.

### Changed
- **Tool calls in a turn run concurrently.** They were sequential despite the
  provider being asked for parallel calls, so a turn cost the sum of its tools.
- **The prompt is laid out for caching**, stable first and clock last. Measured
  on Sonnet: 0 → 3,781 cached tokens re-read per turn.
- **Postgres is optional.** Chat never touches it, so an outage is degraded, not
  down, and the pool reconnects with backoff.
- **The browser is Playwright in-process**, not a globally-installed Node CLI.
- **`web_search` uses keyed providers**, falling back to a real browser. The
  keyless HTML scrape it relied on is now bot-blocked.
- `DB_POOL_MAX` 20 → 5. Chat does not use the pool, and the old default fit only
  about two replicas into a stock Postgres.

### Fixed
- `config.py` **required** `DATABASE_URL` at import while the docs said Postgres
  was optional, so a fresh install crashed before it could explain itself.
- **Backpressure**: bounded in-flight completions, `503` + `Retry-After`, and
  slots released even when a client hangs up mid-stream.
- **Request timeouts** on the model path, and a pooled streaming client instead
  of one per completion.
- **SSRF guard** on every model-chosen URL. Fails closed; catches CGNAT, which
  `ipaddress.is_private` reports as public.

### Security
- Host-access tools are a separate privilege from authentication.
- Unconfined file reads are host-gated, like `shell`.
- Declarative tools cannot take a `user_id`, move their request host, or become
  host tools.
