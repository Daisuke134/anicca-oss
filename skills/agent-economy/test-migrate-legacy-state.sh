#!/usr/bin/env bash
set -euo pipefail

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
INSTANCE="$TMP_ROOT/instance"
OWNER="$TMP_ROOT/owner"
TARGET="$TMP_ROOT/target"
mkdir -p "$INSTANCE/skills/earn/state" "$INSTANCE/.blockrun" "$OWNER/.hermes/state"
printf '%s\n' ledger > "$INSTANCE/skills/earn/state/earn-ledger.jsonl"
printf '%s\n' receipts > "$INSTANCE/skills/earn/state/revenue-receipts.jsonl"
printf '%s\n' compute > "$INSTANCE/.blockrun/compute-receipts.jsonl"
printf '%s\n' shelter > "$OWNER/.hermes/state/shelter-cost.jsonl"

AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" \
AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" \
AGENT_ECONOMY_STATE_ROOT="$TARGET" \
  "$(dirname "$0")/migrate-legacy-state.sh"

cmp "$INSTANCE/skills/earn/state/earn-ledger.jsonl" "$TARGET/earn-ledger.jsonl"
cmp "$INSTANCE/skills/earn/state/revenue-receipts.jsonl" "$TARGET/revenue-receipts.jsonl"
cmp "$INSTANCE/.blockrun/compute-receipts.jsonl" "$TARGET/compute-receipts.jsonl"
cmp "$OWNER/.hermes/state/shelter-cost.jsonl" "$TARGET/shelter-cost.jsonl"
test -f "$INSTANCE/skills/earn/state/earn-ledger.jsonl"

printf '%s\n' keep-target > "$TARGET/earn-ledger.jsonl"
AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" \
AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" \
AGENT_ECONOMY_STATE_ROOT="$TARGET" \
  "$(dirname "$0")/migrate-legacy-state.sh"
test "$(cat "$TARGET/earn-ledger.jsonl")" = keep-target
