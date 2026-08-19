#!/usr/bin/env bash
# Run proteus with uvicorn (multi-worker). Each worker is its own async event loop
# and its own DB pool; scale workers up to ~2× cores for I/O-bound load.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; [ -f .env ] && . ./.env; set +a

# --loop uvloop / --http httptools are pinned rather than left to "auto" so a
# missing extra degrades loudly instead of silently halving throughput.
exec ./.venv/bin/python -m uvicorn app.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-18791}" \
  --workers "${WORKERS:-4}" \
  --loop uvloop \
  --http httptools \
  --no-access-log \
  --timeout-keep-alive 75
