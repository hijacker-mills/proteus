#!/usr/bin/env bash
# Create the virtualenv, install proteus, and put the `proteus` CLI on PATH.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
LINK_DIR="${LINK_DIR:-$HOME/.local/bin}"

if [ ! -d .venv ]; then
  echo "Creating virtualenv (.venv)…"
  "$PYTHON" -m venv .venv
fi

echo "Installing proteus…"
./.venv/bin/pip install --upgrade pip -q
# Editable, so `proteus` reflects the working tree rather than a stale copy —
# which is what you want when the thing you are running IS the checkout.
./.venv/bin/pip install -e . -q

# The console script lands inside .venv/bin, which is not on PATH unless the
# venv is activated. Symlinking it means `proteus` works from any directory,
# and it still resolves agents/, tools/ and .env from THIS checkout, because
# the CLI locates them relative to its own module rather than the cwd.
if [ -d "$LINK_DIR" ] || mkdir -p "$LINK_DIR" 2>/dev/null; then
  ln -sf "$(pwd)/.venv/bin/proteus" "$LINK_DIR/proteus"
  echo "Linked: $LINK_DIR/proteus"
  case ":${PATH}:" in
    *":${LINK_DIR}:"*) ;;
    *) echo "NOTE: $LINK_DIR is not on your PATH. Add it, or run ./.venv/bin/proteus" ;;
  esac
fi

# The browser tool needs a real Chromium. Optional: everything else works
# without it, and web_search falls back to a keyed provider.
if ./.venv/bin/python -c "import playwright" 2>/dev/null; then
  echo "Installing Chromium for the browser tool…"
  ./.venv/bin/playwright install chromium >/dev/null 2>&1 || \
    echo "NOTE: 'playwright install chromium' failed; the browser tool will report it."
fi

cat <<'EOF'

Done.

  cp .env.example .env     # set API_KEY, MODEL and a provider key
  proteus doctor           # check the config, and say what is wrong
  proteus serve            # run the gateway
EOF
