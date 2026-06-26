#!/usr/bin/env bash
# End-to-end smoke test against a running acag instance.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; [ -f .env ] && . ./.env; set +a

BASE="http://127.0.0.1:${PORT:-18791}"
USER_ID="${1:-qubi_test_alice}"

echo "== /healthz =="
curl -s "$BASE/healthz" | python3 -m json.tool

echo; echo "== non-streaming chat ($USER_ID) =="
curl -s "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Qubi-User-Id: $USER_ID" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"What notes do I have on sorting algorithms? Be brief."}]}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('TEXT:', d['choices'][0]['message']['content'][:300]); print('TOOLS:', [e['tool'] for e in d.get('qubi_tool_events',[])])"

echo; echo "== streaming chat ($USER_ID) =="
curl -sN "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Qubi-User-Id: $USER_ID" \
  -H "Content-Type: application/json" \
  -d '{"stream":true,"messages":[{"role":"user","content":"In one sentence, what do you remember about me?"}]}' \
  | grep "^data:" | head -20
