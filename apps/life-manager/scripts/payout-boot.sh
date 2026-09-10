#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME}/.local/state/life-manager/.env}"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/load-env-file.sh"
lm_load_env_file "$ENV_FILE"

if [ "$#" -eq 0 ]; then
  if [ -z "${LM_TENANT_UID:-}" ]; then
    printf '%s\n' '{"status":"skipped","reason":"tenant_not_configured"}'
    exit 0
  fi
  case "$LM_TENANT_UID" in
    *[!A-Za-z0-9_-]*|'') echo "LM_TENANT_UID is invalid" >&2; exit 78 ;;
  esac
  if [ "${#LM_TENANT_UID}" -gt 128 ]; then
    echo "LM_TENANT_UID is invalid" >&2
    exit 78
  fi
  set -- --uid "$LM_TENANT_UID"
fi

export AGENT_WALLET_ADDRESS="${AGENT_WALLET_ADDRESS:-0x477EeE969ccfdc0e959F38cE8B83e372FC0262ad}"
export LM_AGENT_WALLET_PATH="${LM_AGENT_WALLET_PATH:-${HOME}/.cloak/life-manager-agent-wallet.json}"
export LM_PAYOUT_RESERVE_USDC_ATOMIC="${LM_PAYOUT_RESERVE_USDC_ATOMIC:-35000000}"
export LM_PAYOUT_FACILITATOR_URL="${LM_PAYOUT_FACILITATOR_URL:-http://127.0.0.1:8406}"
export LM_PAYOUT_FACILITATOR_START="${LM_PAYOUT_FACILITATOR_START:-${REPO_ROOT}/services/facilitator/start.sh}"

LIFE_MANAGER_NODE="${LIFE_MANAGER_NODE:-$(command -v node || true)}"
[ -n "$LIFE_MANAGER_NODE" ] && [ "${LIFE_MANAGER_NODE#/}" != "$LIFE_MANAGER_NODE" ] && [ -x "$LIFE_MANAGER_NODE" ] || {
  echo "managed node executable is unavailable" >&2
  exit 78
}
LIFE_MANAGER_PYTHON="${LIFE_MANAGER_PYTHON:-$(command -v python3 || true)}"
[ -n "$LIFE_MANAGER_PYTHON" ] && [ "${LIFE_MANAGER_PYTHON#/}" != "$LIFE_MANAGER_PYTHON" ] && [ -x "$LIFE_MANAGER_PYTHON" ] || {
  echo "managed Python executable is unavailable" >&2
  exit 78
}

exec "$LIFE_MANAGER_PYTHON" "$REPO_ROOT/runtime/run-with-timeout.py" 240 "$LIFE_MANAGER_NODE" \
  "$SCRIPT_DIR/run-agent-payout.js" "$@"
