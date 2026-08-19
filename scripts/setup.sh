#!/usr/bin/env bash
#
# Install proteus: virtualenv, dependencies, and the `proteus` CLI on PATH.
#
# Safe to re-run. Every step checks its result and says what to do when it
# fails, because "Installing…" followed by a stack trace is not an error
# message. Nothing here needs root.
#
#   bash scripts/setup.sh                 # normal
#   bash scripts/setup.sh --with-browser  # + Chromium for the browser tool
#   bash scripts/setup.sh --recreate      # rebuild a broken venv from scratch
#   bash scripts/setup.sh --help
#
set -Eeuo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"

PYTHON="${PYTHON:-python3}"
LINK_DIR="${LINK_DIR:-$HOME/.local/bin}"
MIN_MAJOR=3
MIN_MINOR=11

WITH_BROWSER=0
RECREATE=0

# ── output ───────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi
step() { printf '%s==>%s %s\n' "$B" "$N" "$1"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '\n%serror:%s %s\n' "$R" "$N" "$1" >&2; [ $# -gt 1 ] && printf '\n%s\n' "$2" >&2; exit 1; }

# Any unhandled failure names the line rather than vanishing.
trap 'die "setup failed at line $LINENO (command: ${BASH_COMMAND})" "Re-run with: bash -x scripts/setup.sh"' ERR

usage() {
  sed -n '3,14p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --with-browser) WITH_BROWSER=1 ;;
    --recreate)     RECREATE=1 ;;
    -h|--help)      usage ;;
    *) die "unknown option: $1" "Run: bash scripts/setup.sh --help" ;;
  esac
  shift
done

# ── 1. python ────────────────────────────────────────────────────────────────
step "Checking Python"
command -v "$PYTHON" >/dev/null 2>&1 || die \
  "$PYTHON not found." \
  "Install Python ${MIN_MAJOR}.${MIN_MINOR}+, or point PYTHON at it:
    PYTHON=/usr/bin/python3.12 bash scripts/setup.sh"

PY_VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PYTHON" - "$MIN_MAJOR" "$MIN_MINOR" <<'EOF' || die "Python $PY_VER is too old; proteus needs 3.11+." "Install a newer Python and re-run with PYTHON=/path/to/python3.12"
import sys
need = (int(sys.argv[1]), int(sys.argv[2]))
sys.exit(0 if sys.version_info[:2] >= need else 1)
EOF
ok "Python $PY_VER ($(command -v "$PYTHON"))"

# venv is a separate package on Debian/Ubuntu and its absence is the single
# most common failure here, with a useless default error.
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || die \
  "Python is missing the venv/ensurepip modules." \
  "On Debian/Ubuntu:  sudo apt install python${PY_VER}-venv"

# ── 2. virtualenv ────────────────────────────────────────────────────────────
step "Preparing virtualenv"
if [ "$RECREATE" = 1 ] && [ -d .venv ]; then
  rm -rf .venv && ok "removed the old .venv"
fi

# A venv left half-created (interrupted install, moved directory) fails later in
# confusing ways, so check it actually works rather than that the directory exists.
if [ -d .venv ] && ! ./.venv/bin/python -c 'import sys' >/dev/null 2>&1; then
  warn "existing .venv is broken; recreating"
  rm -rf .venv
fi

if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv || die \
    "could not create the virtualenv." \
    "Check free disk (df -h .) and permissions on $REPO"
  ok "created .venv"
else
  ok "reusing .venv"
fi

VENV_PY="$REPO/.venv/bin/python"
[ -x "$VENV_PY" ] || die "the virtualenv has no python at $VENV_PY" "Re-run: bash scripts/setup.sh --recreate"

# ── 3. dependencies ──────────────────────────────────────────────────────────
step "Installing dependencies"
"$VENV_PY" -m pip install --upgrade pip -q 2>/dev/null || warn "could not upgrade pip; continuing"

if ! "$VENV_PY" -m pip install -e . -q; then
  die "dependency installation failed." \
"Common causes:
  * no network, or a proxy needing PIP_INDEX_URL / HTTPS_PROXY
  * no disk space           -> df -h .
  * missing build tools     -> sudo apt install build-essential python${PY_VER}-dev

Re-run verbosely to see the real error:
  ./.venv/bin/pip install -e ."
fi
ok "installed proteus and its dependencies (editable)"

# Import the app for real. A successful pip install can still leave something
# unimportable (a C extension for the wrong Python, a half-written wheel), and
# it is better to find that now than on the first request.
if ! IMPORT_ERR="$("$VENV_PY" -c 'import app.main' 2>&1)"; then
  die "proteus installed but will not import." "$IMPORT_ERR"
fi
ok "app imports cleanly"

# ── 4. the CLI on PATH ───────────────────────────────────────────────────────
step "Installing the CLI"
[ -x "$REPO/.venv/bin/proteus" ] || die \
  "the console script was not created." \
  "This usually means an old setuptools. Try:
    ./.venv/bin/pip install --upgrade setuptools && bash scripts/setup.sh"

if mkdir -p "$LINK_DIR" 2>/dev/null && ln -sf "$REPO/.venv/bin/proteus" "$LINK_DIR/proteus" 2>/dev/null; then
  ok "linked $LINK_DIR/proteus"
  case ":${PATH}:" in
    *":${LINK_DIR}:"*) ok "$LINK_DIR is on PATH" ;;
    *) warn "$LINK_DIR is NOT on your PATH. Add this to ~/.bashrc:"
       printf '        export PATH="%s:$PATH"\n' "$LINK_DIR" ;;
  esac
else
  warn "could not link into $LINK_DIR; use ./.venv/bin/proteus (or set LINK_DIR)"
fi

"$REPO/.venv/bin/proteus" --version >/dev/null 2>&1 \
  && ok "CLI runs: $("$REPO/.venv/bin/proteus" --version)" \
  || die "the CLI is installed but will not run." "Try: ./.venv/bin/proteus --help"

# ── 5. optional browser ──────────────────────────────────────────────────────
if [ "$WITH_BROWSER" = 1 ]; then
  step "Installing Chromium for the browser tool"
  "$VENV_PY" -m pip install -q 'playwright>=1.40' || die "could not install playwright."
  if "$VENV_PY" -m playwright install chromium >/dev/null 2>&1; then
    ok "Chromium installed"
  else
    warn "Chromium download failed. The browser tool will report this clearly."
    warn "Retry later with: ./.venv/bin/playwright install chromium"
  fi
  # Chromium needs shared libraries that are absent from slim images.
  "$VENV_PY" - <<'EOF' 2>/dev/null || warn "Chromium is installed but could not launch. On a slim image run: sudo ./.venv/bin/playwright install-deps chromium"
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        await b.close()
asyncio.run(main())
EOF
fi

# ── 6. config ────────────────────────────────────────────────────────────────
step "Checking configuration"
if [ ! -f .env ]; then
  cp .env.example .env
  ok "created .env from .env.example"
  warn "it has no API_KEY or model credentials yet — edit it before serving"
else
  ok ".env already exists (left untouched)"
fi

trap - ERR
printf '\n%sReady.%s\n\n' "$G" "$N"
cat <<EOF
  proteus doctor     check the config and connectivity, and say what is wrong
  proteus serve      run the gateway
  proteus --help     everything else

EOF
if [ "$WITH_BROWSER" = 0 ]; then
  echo "  The browser tool needs Chromium: bash scripts/setup.sh --with-browser"
  echo
fi
