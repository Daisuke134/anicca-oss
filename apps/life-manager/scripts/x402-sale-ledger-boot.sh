#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME}/.local/state/life-manager/.env}"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/load-env-file.sh"
lm_load_env_file "$ENV_FILE"

export X402_STATE_DIR="${X402_STATE_DIR:-${HOME}/.local/state/life-manager/x402-sell}"
export X402_SELL_STATE_DIR="${X402_SELL_STATE_DIR:-${X402_STATE_DIR}}"
export X402_SELF_WALLETS_MODULE="${X402_SELF_WALLETS_MODULE:-${REPO_ROOT}/skills/earn/x402-sell/lib/self-wallets.mjs}"
export LM_FINANCIAL_RECORDS_DIR="${LM_FINANCIAL_RECORDS_DIR:-${CFO_STATE_DIR:-${HOME}/.local/state/life-manager/life-manager-cfo-hourly}/financial-records}"

exec /opt/homebrew/bin/timeout 240 /opt/homebrew/bin/node \
  "$SCRIPT_DIR/record-x402-sales.js" \
  --state-dir "$X402_SELL_STATE_DIR"
