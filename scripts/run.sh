#!/usr/bin/env bash
# Run acag with uvicorn (multi-worker). Each worker is its own async event loop
# and its own DB pool; scale workers up to ~2× cores for I/O-bound load.
set -euo pipefail
cd "$(dirname "$0")/.."

set -a; [ -f .env ] && . ./.env; set +a

exec ./.venv/bin/python -m uvicorn app.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-18791}" \
  --workers "${WORKERS:-4}" \
  --no-access-log \
  --timeout-keep-alive 75
