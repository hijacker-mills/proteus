# Contributing

## Running the tests

```bash
bash scripts/setup.sh
pytest tests/test_units.py        # fast, needs nothing
```

`tests/test_units.py` is the suite CI runs. It must stay runnable with **no
database, no gateway, no provider key and no network**, in a few seconds. That
constraint is the whole point: a suite that needs Postgres is a suite people
skip, and a skipped suite protects nothing.

Everything else in `tests/` is a live-integration script, run by hand:

```bash
set -a; . ./.env; set +a
python tests/phase1_verify.py       # tool concurrency, ordering, prompt layout
python tests/declarative_tools.py   # file-defined tool security
python tests/real_model.py          # needs a real provider; spends tokens
MODEL="mock/tool?tokens=400&delay_ms=25" WORKERS=1 proteus serve &
python tests/phase1_live.py         # needs a running gateway
```

## Where things go

| adding | goes in |
|---|---|
| an agent | `agents/<name>.md` — no code |
| a tool that calls an HTTP API | `tools/<name>.md` — no code |
| a tool with real logic | `app/tools/custom/<name>.py` |
| a built-in toolset | register it in `toolsets._provider()` |
| provider support | `app/llm.py` only — it is the sole provider-aware module |

## Things that are load-bearing

Change these deliberately, not incidentally:

- **`user_id` is injected server-side** and is never a tool parameter. That is
  what stops one user's request reaching another's data, and it does not depend
  on the model behaving.
- **Host tools are a separate privilege** from authentication. `API_KEY` is
  shared with every product surface, so it authenticates a surface, not an
  operator. See `toolsets.HOST_TOOLS`.
- **The SSRF guard fails closed.** DNS failure, an unparseable address and an
  unexpected exception all block. Cloud metadata stays blocked whatever the
  configuration says.
- **Tool results go back in call order**, even though tools run concurrently.
  Providers match `tool_call_id` positionally and reject a shuffled list, and
  this only fails against a real provider.
- **The system prompt is assembled stable-first, clock last.** Reordering it
  silently destroys prompt caching, which no test will fail on and the bill will.

## Style

Match the surrounding code. Comments explain *why*, especially where something
looks odd — most of the odd-looking code here is odd because the obvious version
was wrong, and the comment is the only record of that.
