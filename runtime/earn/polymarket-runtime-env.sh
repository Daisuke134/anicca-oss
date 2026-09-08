#!/usr/bin/env bash
# Shared install/runtime contract for every active Polymarket loop.

PM_RUNTIME_REPO="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)" || return 2
PM_RUNTIME_ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME:?}/.local/state/life-manager/.env}"
_PM_PYTHON_CALLER="${LIFE_MANAGER_PYTHON:-$HOME/.local/share/life-manager/venv/bin/python}"
_PM_NODE_CALLER="${LIFE_MANAGER_NODE:-$(command -v node 2>/dev/null || true)}"
_PM_STATE_CALLER="${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/polymarket}"
_PM_KILL_CALLER="${PM_KILL_SWITCH:-$HOME/.local/state/life-manager/polymarket/KILL}"
_PM_LEDGER_CALLER="${LIFE_MANAGER_EARN_LEDGER_PATH:-$HOME/.local/state/life-manager/earn/earn-ledger.jsonl}"
_PM_RELAYER_CALLER="${LIFE_MANAGER_RELAYER_CACHE:-}"
if [[ -n "${LIFE_MANAGER_ENV_FILE:-}" && ! -f "$PM_RUNTIME_ENV_FILE" ]]; then
  echo "LIFE_MANAGER_ENV_FILE does not exist" >&2
  return 2
fi
if [[ -f "$PM_RUNTIME_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$PM_RUNTIME_ENV_FILE" || { set +a; echo "failed to load LIFE_MANAGER_ENV_FILE" >&2; return 2; }
  set +a
fi

LIFE_MANAGER_ENV_FILE="$PM_RUNTIME_ENV_FILE"
LIFE_MANAGER_PYTHON="$_PM_PYTHON_CALLER"
LIFE_MANAGER_NODE="$_PM_NODE_CALLER"
LIFE_MANAGER_STATE_ROOT="$_PM_STATE_CALLER"
LIFE_MANAGER_WALLET_HOME="${LIFE_MANAGER_WALLET_HOME:-}"
PM_DEPOSIT_WALLET="${PM_DEPOSIT_WALLET:-}"
PM_KILL_SWITCH="$_PM_KILL_CALLER"
LIFE_MANAGER_EARN_LEDGER_PATH="$_PM_LEDGER_CALLER"
LIFE_MANAGER_RELAYER_CACHE="${_PM_RELAYER_CALLER:-$LIFE_MANAGER_STATE_ROOT/relayer-apikey}"
unset _PM_PYTHON_CALLER _PM_NODE_CALLER _PM_STATE_CALLER _PM_KILL_CALLER
unset _PM_LEDGER_CALLER _PM_RELAYER_CALLER

[[ -x "$LIFE_MANAGER_PYTHON" ]] || { echo "managed LIFE_MANAGER_PYTHON is not executable" >&2; return 2; }
[[ "$LIFE_MANAGER_NODE" == /* && -x "$LIFE_MANAGER_NODE" ]] || {
  echo "managed LIFE_MANAGER_NODE must be an absolute executable" >&2; return 2;
}
[[ "$LIFE_MANAGER_WALLET_HOME" == /* && -d "$LIFE_MANAGER_WALLET_HOME" ]] || {
  echo "LIFE_MANAGER_WALLET_HOME must be an existing absolute directory" >&2; return 2;
}
[[ "$PM_DEPOSIT_WALLET" =~ ^0x[0-9a-fA-F]{40}$ ]] || {
  echo "PM_DEPOSIT_WALLET must be a 0x-prefixed 40-hex address" >&2; return 2;
}

_pm_realpath() {
  "$LIFE_MANAGER_PYTHON" -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve(strict=False))' "$1"
}
_pm_external_path() {
  local name="$1" raw="$2" resolved
  [[ "$raw" == /* ]] || { echo "$name must be absolute" >&2; return 2; }
  resolved="$(_pm_realpath "$raw")" || return 2
  [[ "$resolved" != "$PM_RUNTIME_REPO" && "$resolved/" != "$PM_RUNTIME_REPO/"* ]] || {
    echo "$name must resolve outside the repository" >&2; return 2;
  }
  printf '%s\n' "$resolved"
}
LIFE_MANAGER_STATE_ROOT="$(_pm_external_path LIFE_MANAGER_STATE_ROOT "$LIFE_MANAGER_STATE_ROOT")" || return 2
PM_KILL_SWITCH="$(_pm_external_path PM_KILL_SWITCH "$PM_KILL_SWITCH")" || return 2
LIFE_MANAGER_EARN_LEDGER_PATH="$(_pm_external_path LIFE_MANAGER_EARN_LEDGER_PATH "$LIFE_MANAGER_EARN_LEDGER_PATH")" || return 2
LIFE_MANAGER_RELAYER_CACHE="$(_pm_external_path LIFE_MANAGER_RELAYER_CACHE "$LIFE_MANAGER_RELAYER_CACHE")" || return 2

_pm_inherited_pkvar="${PKVAR:-}"
_pm_unset=(-u ANICCA_EVM_PRIVATE_KEY -u BASE_CHAIN_WALLET_KEY -u BLOCKRUN_WALLET_KEY -u POLYGON_WALLET_PRIVATE_KEY -u PKVAR)
[[ -z "$_pm_inherited_pkvar" ]] || _pm_unset+=(-u "$_pm_inherited_pkvar")
_pm_resolved_key="$(env "${_pm_unset[@]}" ANICCA_HOME="$LIFE_MANAGER_WALLET_HOME" \
  "$LIFE_MANAGER_NODE" "$PM_RUNTIME_REPO/runtime/earn/lib/resolve-identity.mjs" evm 2>/dev/null)"
[[ "$_pm_resolved_key" =~ ^0x[0-9a-fA-F]{64}$ ]] || {
  echo "install EVM key was not resolvable" >&2; return 2;
}
unset ANICCA_EVM_PRIVATE_KEY BASE_CHAIN_WALLET_KEY BLOCKRUN_WALLET_KEY PKVAR
case "$_pm_inherited_pkvar" in
  ""|ANICCA_HOME|HOME|PATH|SHELL|LIFE_MANAGER_*|PM_*) ;;
  *) unset "$_pm_inherited_pkvar" ;;
esac
unset _pm_inherited_pkvar _pm_unset
ANICCA_HOME="$LIFE_MANAGER_WALLET_HOME"
POLYGON_WALLET_PRIVATE_KEY="$_pm_resolved_key"
BLOCKRUN_WALLET_KEY="$POLYGON_WALLET_PRIVATE_KEY"
unset _pm_resolved_key

export LIFE_MANAGER_REPO="$PM_RUNTIME_REPO" LIFE_MANAGER_ENV_FILE LIFE_MANAGER_PYTHON LIFE_MANAGER_NODE
export ANICCA_HOME
export LIFE_MANAGER_STATE_ROOT LIFE_MANAGER_WALLET_HOME PM_DEPOSIT_WALLET
export PM_KILL_SWITCH LIFE_MANAGER_EARN_KILL_PATH="$PM_KILL_SWITCH"
export LIFE_MANAGER_EARN_LEDGER_PATH PM_LEDGER_PATH="$LIFE_MANAGER_EARN_LEDGER_PATH"
export LIFE_MANAGER_RELAYER_CACHE POLYGON_WALLET_PRIVATE_KEY
export BLOCKRUN_WALLET_KEY
mkdir -p "$LIFE_MANAGER_STATE_ROOT" "$(dirname "$PM_KILL_SWITCH")" \
  "$(dirname "$LIFE_MANAGER_EARN_LEDGER_PATH")" "$(dirname "$LIFE_MANAGER_RELAYER_CACHE")"
