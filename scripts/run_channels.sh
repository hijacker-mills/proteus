#!/usr/bin/env bash
# Run the single-process channel poller (Telegram long-poll, Signal receive).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
exec ./.venv/bin/python -m app.channels_runner
