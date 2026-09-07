#!/usr/bin/env bash
set -euo pipefail

TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT
INSTANCE="$TMP_ROOT/instance"
TARGET="$INSTANCE/state/skills"
mkdir -p "$INSTANCE/skills/earn/state/nested" "$INSTANCE/skills/economy/gig/state"
printf '%s\n' ledger > "$INSTANCE/skills/earn/state/earn-ledger.jsonl"
printf '%s\n' nested > "$INSTANCE/skills/earn/state/nested/evidence.json"
printf '%s\n' seen > "$INSTANCE/skills/economy/gig/state/seen.json"

ANICCA_HOME="$INSTANCE" "$(dirname "$0")/migrate-legacy-skill-state.sh"
cmp "$INSTANCE/skills/earn/state/earn-ledger.jsonl" "$TARGET/earn/earn-ledger.jsonl"
cmp "$INSTANCE/skills/earn/state/nested/evidence.json" "$TARGET/earn/nested/evidence.json"
cmp "$INSTANCE/skills/economy/gig/state/seen.json" "$TARGET/economy/gig/seen.json"
test -f "$INSTANCE/skills/earn/state/earn-ledger.jsonl"

printf '%s\n' keep-target > "$TARGET/earn/earn-ledger.jsonl"
ANICCA_HOME="$INSTANCE" "$(dirname "$0")/migrate-legacy-skill-state.sh"
test "$(cat "$TARGET/earn/earn-ledger.jsonl")" = keep-target
