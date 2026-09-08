#!/bin/bash
# earn-watch — the waiting jobs, so no human has to sit on them.
#   1. external revenue: has this install's configured payee received USDC?
#   2. Polymarket: is this install's configured position redeemable yet? if so, redeem it.
#   3. Bazaar: is the rent-a-box product indexed yet?
# Writes one status line per run; only acts when a condition is actually met.
set -uo pipefail
TS=$(date -u +%FT%TZ)
ENV_FILE=${LIFE_MANAGER_ENV_FILE:-${HOME:-}/.local/state/life-manager/.env}
if [[ -n "${LIFE_MANAGER_ENV_FILE:-}" && ! -f "$ENV_FILE" ]]; then
    echo "LIFE_MANAGER_ENV_FILE does not exist: $LIFE_MANAGER_ENV_FILE" >&2
    exit 2
fi
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  if ! source "$ENV_FILE"; then
    echo "failed to load LIFE_MANAGER_ENV_FILE: $ENV_FILE" >&2
    exit 2
  fi
  set +a
fi
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
NODE=${LIFE_MANAGER_NODE:-node}
PYTHON=${LIFE_MANAGER_PYTHON:-"$HOME/.local/share/life-manager/venv/bin/python"}
STATE_ROOT=${LIFE_MANAGER_STATE_ROOT:-"$HOME/.local/state/life-manager/earn-watch"}
KILL_PATH=${PM_KILL_SWITCH:-"$HOME/.local/state/life-manager/polymarket/KILL"}
WALLET_HOME=${LIFE_MANAGER_WALLET_HOME:-}
PM_WALLET=${PM_DEPOSIT_WALLET:-}
PAYEE=${EARN_WATCH_PAYEE:-}
INHERITED_PKVAR=${PKVAR:-}

if [[ "$WALLET_HOME" != /* || ! -d "$WALLET_HOME" ]]; then
  echo "earn-watch requires an existing absolute LIFE_MANAGER_WALLET_HOME" >&2
  exit 2
fi
if [[ ! "$PM_WALLET" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
  echo "PM_DEPOSIT_WALLET must be a 0x-prefixed 40-hex address" >&2
  exit 2
fi
if [[ ! "$PAYEE" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
  echo "EARN_WATCH_PAYEE must be a 0x-prefixed 40-hex address" >&2
  exit 2
fi
if [[ "$STATE_ROOT" != /* ]]; then
  echo "LIFE_MANAGER_STATE_ROOT must be absolute" >&2
  exit 2
fi
if [[ "$KILL_PATH" != /* ]]; then
  echo "PM_KILL_SWITCH must be absolute" >&2
  exit 2
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "managed LIFE_MANAGER_PYTHON is not executable: $PYTHON" >&2
  exit 2
fi
canonical_path() {
  python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=False))' "$1"
}
REPO_REAL=$(canonical_path "$REPO_ROOT") || exit 2
STATE_ROOT=$(canonical_path "$STATE_ROOT") || exit 2
KILL_PATH=$(canonical_path "$KILL_PATH") || exit 2
outside_repo() {
  [[ "$1" == /* && "$1" != "$REPO_REAL" && "$1/" != "$REPO_REAL/"* ]]
}
if ! outside_repo "$STATE_ROOT"; then
  echo "LIFE_MANAGER_STATE_ROOT must resolve outside the repository" >&2
  exit 2
fi
if ! outside_repo "$KILL_PATH"; then
  echo "PM_KILL_SWITCH must resolve outside the repository" >&2
  exit 2
fi
RESOLVER="$REPO_ROOT/runtime/earn/lib/resolve-identity.mjs"
REDEEM="$REPO_ROOT/skills/earn/polymarket-trade/redeem.py"
if [[ ! -f "$RESOLVER" || ! -f "$REDEEM" ]]; then
  echo "earn-watch repository closure is incomplete" >&2
  exit 2
fi
UNSET_KEY_ENV=(-u ANICCA_EVM_PRIVATE_KEY -u BLOCKRUN_WALLET_KEY -u POLYGON_WALLET_PRIVATE_KEY -u PKVAR)
if [[ -n "$INHERITED_PKVAR" ]]; then
  UNSET_KEY_ENV+=(-u "$INHERITED_PKVAR")
fi

usdc=$(curl -s -m 20 https://base-rpc.publicnode.com -X POST -H "content-type: application/json" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_call\",\"params\":[{\"to\":\"0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913\",\"data\":\"0x70a08231000000000000000000000000${PAYEE:2}\"},\"latest\"]}" \
  | python3 -c "import json,sys;print(int(json.load(sys.stdin).get('result','0x0'),16)/1e6)" 2>/dev/null || echo "?")

red=$(curl -s -m 20 "https://data-api.polymarket.com/positions?user=$PM_WALLET" \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(sum(1 for p in d if p.get('redeemable')))" 2>/dev/null || echo "?")

if [ "$red" != "?" ] && [ "${red:-0}" -gt 0 ]; then
  echo "$TS REDEEMABLE=$red -> redeeming"
  # Resolve only this install's wallet.  Clear inherited key selectors so a sibling
  # loop's signer can never override LIFE_MANAGER_WALLET_HOME.
  K=$(env "${UNSET_KEY_ENV[@]}" \
    ANICCA_HOME="$WALLET_HOME" "$NODE" "$RESOLVER" evm 2>/dev/null)
  if [[ ! "$K" =~ ^0x[0-9a-fA-F]{64}$ ]]; then
    echo "$TS redeem skipped: install EVM key was not resolvable"
  else
    mkdir -p "$STATE_ROOT"
    env "${UNSET_KEY_ENV[@]}" -u PM_TRADE_AGENT_ENV \
      LIFE_MANAGER_REPO="$REPO_ROOT" LIFE_MANAGER_STATE_ROOT="$STATE_ROOT" \
      LIFE_MANAGER_NODE="$NODE" \
      LIFE_MANAGER_EARN_LEDGER_PATH="$STATE_ROOT/earn-ledger.jsonl" \
      LIFE_MANAGER_EARN_KILL_PATH="$KILL_PATH" PM_KILL_SWITCH="$KILL_PATH" \
      LIFE_MANAGER_RELAYER_CACHE="$STATE_ROOT/relayer-apikey" \
      PM_DEPOSIT_WALLET="$PM_WALLET" POLYGON_WALLET_PRIVATE_KEY="$K" \
      "$PYTHON" "$REDEEM" 2>&1 | tail -3
  fi
fi

bz=no
for off in 8000 10000 12000 14000; do
  if curl -s -m 25 "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources?limit=2000&offset=$off" \
     | grep -q "rent-a-box"; then bz=yes; break; fi
done

echo "$TS payee_usdc=$usdc pm_redeemable=$red bazaar_rentabox=$bz"
