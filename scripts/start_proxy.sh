#!/usr/bin/env bash
# Start Paritok proxy against hosted GPU. Keep this terminal open.
# Optional free upstream (Groq): set RAGMOD_OPENAI_URL + OPENAI_API_KEY in .env
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${PARITOK_API_KEY:-}" ]]; then
  echo "PARITOK_API_KEY is not set. Copy .env.example → .env and add your key." >&2
  exit 1
fi

PYTHON=()
if [[ -n "${RAGMOD_PYTHON:-}" ]]; then
  PYTHON=("$RAGMOD_PYTHON")
elif python -c "import sys" >/dev/null 2>&1; then
  PYTHON=(python)
elif python3 -c "import sys" >/dev/null 2>&1; then
  PYTHON=(python3)
elif py -3 -c "import sys" >/dev/null 2>&1; then
  PYTHON=(py -3)
else
  echo "No usable Python interpreter found. Set RAGMOD_PYTHON to your Python executable." >&2
  exit 1
fi

# Inject key into a temp config so the empty api_key in committed yaml is fine.
TMP_CFG="$(mktemp)"
trap 'rm -f "$TMP_CFG"' EXIT
SRC="$ROOT/paritok.yaml" DST="$TMP_CFG" "${PYTHON[@]}" - <<'PY'
import os
import yaml

src, dst = os.environ["SRC"], os.environ["DST"]
with open(src, encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}
cfg["use_gpu_server"] = True
cfg.setdefault("gpu_server", {})["api_key"] = os.environ["PARITOK_API_KEY"]
cfg.setdefault("tool_discovery", {})["strategy"] = "embedding"
with open(dst, "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, default_flow_style=False)
PY

PORT="${RAGMOD_PROXY_PORT:-8080}"
EXTRA=()
# Free OpenAI-compatible upstreams (Groq / Gemini / …)
# Groq:   https://api.groq.com/openai
# Gemini: https://generativelanguage.googleapis.com/v1beta/openai
if [[ -n "${RAGMOD_OPENAI_URL:-}" ]]; then
  EXTRA+=(--openai-url "${RAGMOD_OPENAI_URL}")
  echo "Upstream OpenAI-compat URL: ${RAGMOD_OPENAI_URL}"
fi

# Ragmod sends Authorization from OPENAI_API_KEY or GEMINI_API_KEY.
if [[ -z "${OPENAI_API_KEY:-}" && -n "${GEMINI_API_KEY:-}" ]]; then
  export OPENAI_API_KEY="${GEMINI_API_KEY}"
fi

echo "Starting Paritok proxy on :$PORT (hosted GPU) ..."
echo "Upstream model (RAGMOD_MODEL): ${RAGMOD_MODEL:-unset}"
exec paritok proxy --port "$PORT" --config-file "$TMP_CFG" "${EXTRA[@]}"
