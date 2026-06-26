#!/usr/bin/env bash
# Create the acag virtualenv and install dependencies.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"

if [ ! -d .venv ]; then
  echo "Creating virtualenv (.venv)…"
  "$PYTHON" -m venv .venv
fi

echo "Installing dependencies…"
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install -r requirements.txt -q

echo "Done. Run with:  bash scripts/run.sh"
