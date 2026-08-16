#!/usr/bin/env bash
# Polls .env for valid-looking keys, then starts proxy + runs Wave 0 smoke.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate

ENV_FILE="$ROOT/.env"
echo "Waiting for keys in $ENV_FILE ..."
echo "  Need: PARITOK_API_KEY=pk_live_...  AND  OPENAI_API_KEY=gsk_... (Groq)"
echo "  Or:   PARITOK_API_KEY + OPENAI_API_KEY with RAGMOD_OPENAI_URL set"
echo

have_keys() {
  [[ -f "$ENV_FILE" ]] || return 1
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
  [[ -n "${PARITOK_API_KEY:-}" && "${PARITOK_API_KEY}" == pk_live_* ]] || return 1
  [[ -n "${OPENAI_API_KEY:-}" ]] || return 1
  return 0
}

for i in $(seq 1 180); do  # ~15 min @ 5s
  if have_keys; then
    echo "Keys detected."
    break
  fi
  sleep 5
  if (( i % 6 == 0 )); then
    echo "  still waiting… ($((i*5))s)  edit .env then save"
  fi
  if (( i == 180 )); then
    echo "Timed out waiting for keys." >&2
    exit 1
  fi
done

# Ensure Groq defaults if using gsk_ key and no URL set
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
if [[ "${OPENAI_API_KEY}" == gsk_* ]] && [[ -z "${RAGMOD_OPENAI_URL:-}" ]]; then
  {
    echo "RAGMOD_OPENAI_URL=https://api.groq.com/openai"
    echo "RAGMOD_MODEL=llama-3.1-8b-instant"
  } >> "$ENV_FILE"
  echo "Appended Groq defaults to .env"
fi

pkill -f 'paritok proxy' 2>/dev/null || true
sleep 1

echo "Starting proxy..."
./scripts/start_proxy.sh > /tmp/ragmod-proxy.log 2>&1 &
PROXY_PID=$!
echo "proxy pid $PROXY_PID (log: /tmp/ragmod-proxy.log)"

# Wait for health
for i in $(seq 1 60); do
  if curl -sf http://127.0.0.1:8080/health >/dev/null; then
    echo "Proxy healthy."
    break
  fi
  sleep 1
  if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    echo "Proxy died. Log:" >&2
    cat /tmp/ragmod-proxy.log >&2 || true
    exit 1
  fi
  if (( i == 60 )); then
    echo "Proxy health timeout." >&2
    cat /tmp/ragmod-proxy.log >&2 || true
    exit 1
  fi
done

# Fail fast if hosted GPU key still rejected
if rg -q 'API key check failed|HTTP 401' /tmp/ragmod-proxy.log; then
  echo "Paritok key still rejected (401). Create a fresh key at https://paritok.com" >&2
  rg -n 'WARNING|401|API key' /tmp/ragmod-proxy.log >&2 || true
  exit 1
fi

echo "Running smoke..."
python scripts/wave0_smoke.py
CODE=$?
echo "smoke exit=$CODE"
exit "$CODE"
