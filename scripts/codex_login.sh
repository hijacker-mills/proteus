#!/usr/bin/env bash
# One-time OpenAI Codex (ChatGPT) OAuth login for proteus.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a
export PYTHONUNBUFFERED=1
exec ./.venv/bin/python -u -m app.codex_login
