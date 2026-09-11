#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME}/.local/state/life-manager/.env}"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/load-env-file.sh"
lm_load_env_file "$ENV_FILE"
source "$SCRIPT_DIR/lib/portable-runtime.sh"
lm_prepare_portable_runtime "$REPO_ROOT"

export TASKMARKET_WORKER_ADDRESS="${TASKMARKET_WORKER_ADDRESS:-0xd7Db94062AFec8a86F70250B931C77619acf8937}"
export TASKMARKET_SELF_WALLETS_MODULE="${TASKMARKET_SELF_WALLETS_MODULE:-${REPO_ROOT}/skills/earn/x402-sell/lib/self-wallets.mjs}"
export LIFE_MANAGER_AGENT_WALLET_ADDRESS="${LIFE_MANAGER_AGENT_WALLET_ADDRESS:-0x477EeE969ccfdc0e959F38cE8B83e372FC0262ad}"

LEDGER_RESULT="$(mktemp "${TMPDIR:-/tmp}/life-manager-taskmarket-ledger.XXXXXX")"
trap 'rm -f "$LEDGER_RESULT"' EXIT

lm_timeout 180 "$LM_NODE" \
  "$SCRIPT_DIR/record-taskmarket-work.js" \
  --worker "$TASKMARKET_WORKER_ADDRESS" \
  --task "0x37f67e062d4384c3adf252844545e916128c377569ac418ae46c7cc1a2a97c7d" \
  | tee "$LEDGER_RESULT"

lm_timeout 55 "$LM_NODE" \
  "$SCRIPT_DIR/handoff-taskmarket-awards.js" \
  --ledger-result "$LEDGER_RESULT" \
  --worker "$TASKMARKET_WORKER_ADDRESS" \
  --destination "$LIFE_MANAGER_AGENT_WALLET_ADDRESS"
