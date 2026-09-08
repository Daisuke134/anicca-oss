#!/bin/bash
# reinvest — the compounding half of the loop. Revenue lands in the founder wallet on Base;
# anything above the operating reserve gets deployed to yield automatically. No human decides
# when to reinvest, and no human moves the money.
#
# Reserve exists so the wallet can always pay for compute and inference; only the surplus works.
set -uo pipefail
TS=$(date -u +%FT%TZ)

ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME:-}/.local/state/life-manager/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  if ! . "$ENV_FILE"; then
    echo "reinvest: failed to load LIFE_MANAGER_ENV_FILE" >&2
    exit 78
  fi
  set +a
fi

case "${REINVEST_ANICCA_HOME:-}" in
  /*) reinvest_home="$REINVEST_ANICCA_HOME" ;;
  *) echo "reinvest: REINVEST_ANICCA_HOME must be an absolute path" >&2; exit 78 ;;
esac
# This loop is bound to the wallet stored under REINVEST_ANICCA_HOME. Never inherit a
# direct or indirection-based signing key from another loop's process environment.
borrowed_key_name="${PKVAR:-}"
if [[ "$borrowed_key_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  unset "$borrowed_key_name"
fi
unset PKVAR BLOCKRUN_WALLET_KEY ANICCA_EVM_PRIVATE_KEY
export ANICCA_HOME="$reinvest_home"
export COMPUTE_RESERVE_USDC=${COMPUTE_RESERVE_USDC:-3}
export YIELD_MIN_DEPLOY_USDC=${YIELD_MIN_DEPLOY_USDC:-1}

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# launchd PATH lacks coreutils; call node directly (the script has its own network timeouts)
out=$(PATH=/opt/homebrew/bin:/usr/bin:/bin "${LIFE_MANAGER_NODE:-node}" "$SCRIPT_DIR/execute-yield.mjs" 2>&1 | tail -1)
echo "$TS $out"
