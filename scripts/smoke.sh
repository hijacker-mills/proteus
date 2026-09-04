#!/usr/bin/env bash
# End-to-end smoke test against a running proteus instance.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a

BASE="http://127.0.0.1:${PORT:-18791}"
USER_ID="${1:-smoketest-user}"

IDENTITY_ARGS=()
if [[ -n "${PROTEUS_IDENTITY_SECRET:-}" ]]; then
  IDENTITY_TIMESTAMP="$(date +%s)"
  IDENTITY_SIGNATURE="$(printf '%s:%s' "$USER_ID" "$IDENTITY_TIMESTAMP" | openssl dgst -sha256 -hmac "$PROTEUS_IDENTITY_SECRET" -hex | sed 's/^.* //')"
  IDENTITY_ARGS=(-H "X-Proteus-Identity-Timestamp: $IDENTITY_TIMESTAMP" -H "X-Proteus-Identity-Signature: $IDENTITY_SIGNATURE")
fi

echo "== /healthz =="
curl -s "$BASE/healthz" | python3 -m json.tool

echo; echo "== non-streaming chat ($USER_ID) =="
curl -s "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Proteus-User-Id: $USER_ID" \
  "${IDENTITY_ARGS[@]}" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"Say PONG and nothing else."}]}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('TEXT:', d['choices'][0]['message']['content'][:300]); print('TOOLS:', [e['tool'] for e in d.get('proteus_tool_events',[])])"

echo; echo "== streaming chat ($USER_ID) =="
curl -sN "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Proteus-User-Id: $USER_ID" \
  "${IDENTITY_ARGS[@]}" \
  -H "Content-Type: application/json" \
  -d '{"stream":true,"messages":[{"role":"user","content":"In one sentence, what is a gateway?"}]}' \
  | grep "^data:" | head -20
