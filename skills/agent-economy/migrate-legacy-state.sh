#!/usr/bin/env bash
set -euo pipefail

INSTANCE_HOME="${AGENT_ECONOMY_LEGACY_INSTANCE_HOME:-${ANICCA_HOME:-$HOME/.anicca}}"
OWNER_HOME="${AGENT_ECONOMY_LEGACY_OWNER_HOME:-$HOME}"
TARGET="${AGENT_ECONOMY_STATE_ROOT:-${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager}/agent-economy}"
OLD_EARN="$INSTANCE_HOME/skills/earn/state"

mkdir -p "$TARGET"

copy_once() {
  local source="$1" target="$2"
  [ -f "$source" ] || return 0
  [ ! -e "$target" ] || return 0
  cp -p "$source" "$target"
}

for name in earn-ledger.jsonl receipt-reconciliations.jsonl revenue-receipts.inbox.jsonl revenue-receipts.jsonl; do
  copy_once "$OLD_EARN/$name" "$TARGET/$name"
done
copy_once "$INSTANCE_HOME/.blockrun/compute-receipts.jsonl" "$TARGET/compute-receipts.jsonl"
copy_once "$OWNER_HOME/.hermes/state/shelter-cost.jsonl" "$TARGET/shelter-cost.jsonl"

echo "Agent Economy legacy state copied to $TARGET; source retained."
