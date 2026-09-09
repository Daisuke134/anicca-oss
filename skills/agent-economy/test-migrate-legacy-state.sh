#!/usr/bin/env bash
set -euo pipefail

TMP_ROOT="$(mktemp -d /private/tmp/lm-agent-economy-migration.XXXXXX)"
trap 'rm -rf "$TMP_ROOT"' EXIT
INSTANCE="$TMP_ROOT/instance"
OWNER="$TMP_ROOT/owner"
TARGET="$OWNER/.local/state/life-manager/agent-economy"
mkdir -p "$INSTANCE/skills/earn/state" "$INSTANCE/.blockrun" "$INSTANCE/.automaton" \
  "$INSTANCE/identity" "$INSTANCE/state" "$OWNER/.hermes/state"
printf '%s\n' ledger > "$INSTANCE/skills/earn/state/earn-ledger.jsonl"
printf '%s\n' receipts > "$INSTANCE/skills/earn/state/revenue-receipts.jsonl"
printf '%s\n' compute > "$INSTANCE/.blockrun/compute-receipts.jsonl"
printf '%s\n' shelter > "$OWNER/.hermes/state/shelter-cost.jsonl"
printf '%s\n' citizens > "$OWNER/.hermes/state/citizens.json"
printf '%s\n' children > "$OWNER/.hermes/state/children.jsonl"
printf '%s\n' pending > "$OWNER/.hermes/state/pending-registry-appends.jsonl"
printf '%s\n' wallet > "$INSTANCE/.automaton/wallet.json"
printf '%s\n' env > "$INSTANCE/.env"
printf '%s\n' genesis > "$INSTANCE/identity/genesis.md"
printf '%s\n' name > "$INSTANCE/identity/name"
printf '%s\n' wake > "$INSTANCE/state/ledger.jsonl"
printf '%s\n' failure > "$INSTANCE/state/harness-failures.jsonl"

AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" \
AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" \
AGENT_ECONOMY_STATE_ROOT="$TARGET" \
  "$(dirname "$0")/migrate-legacy-state.sh"

cmp "$INSTANCE/skills/earn/state/earn-ledger.jsonl" "$TARGET/earn-ledger.jsonl"
cmp "$INSTANCE/skills/earn/state/revenue-receipts.jsonl" "$TARGET/revenue-receipts.jsonl"
cmp "$INSTANCE/.blockrun/compute-receipts.jsonl" "$TARGET/compute-receipts.jsonl"
cmp "$OWNER/.hermes/state/shelter-cost.jsonl" "$TARGET/shelter-cost.jsonl"
cmp "$OWNER/.hermes/state/citizens.json" "$TARGET/instance/state/citizens.json"
cmp "$OWNER/.hermes/state/children.jsonl" "$TARGET/instance/state/children.jsonl"
cmp "$OWNER/.hermes/state/pending-registry-appends.jsonl" \
  "$TARGET/instance/state/pending-registry-appends.jsonl"
cmp "$OWNER/.hermes/state/shelter-cost.jsonl" "$TARGET/instance/state/shelter-cost.jsonl"
cmp "$INSTANCE/.automaton/wallet.json" "$TARGET/instance/.automaton/wallet.json"
cmp "$INSTANCE/.env" "$TARGET/instance/.env"
cmp "$INSTANCE/identity/genesis.md" "$TARGET/instance/identity/genesis.md"
cmp "$INSTANCE/identity/name" "$TARGET/instance/identity/name"
cmp "$INSTANCE/state/ledger.jsonl" "$TARGET/instance/state/ledger.jsonl"
cmp "$INSTANCE/state/harness-failures.jsonl" "$TARGET/instance/state/harness-failures.jsonl"
cmp "$INSTANCE/skills/earn/state/earn-ledger.jsonl" \
  "$TARGET/instance/state/skills/earn/earn-ledger.jsonl"
test -f "$INSTANCE/skills/earn/state/earn-ledger.jsonl"
test "$(stat -f%Lp "$TARGET")" = 700
test "$(stat -f%Lp "$TARGET/earn-ledger.jsonl")" = 600
test "$(stat -f%Lp "$TARGET/instance/.automaton/wallet.json")" = 600

printf '%s\n' keep-target > "$TARGET/earn-ledger.jsonl"
AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" \
AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" \
AGENT_ECONOMY_STATE_ROOT="$TARGET" \
  "$(dirname "$0")/migrate-legacy-state.sh"
test "$(cat "$TARGET/earn-ledger.jsonl")" = keep-target
test "$(stat -f%Lp "$TARGET/earn-ledger.jsonl")" = 600

if AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" AGENT_ECONOMY_STATE_ROOT="$INSTANCE" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "overlapping destination was accepted" >&2
  exit 1
fi

OUTSIDE="$TMP_ROOT/outside"
mkdir -m 755 "$OUTSIDE"
ln -s "$OUTSIDE" "$TMP_ROOT/target-link"
if AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" AGENT_ECONOMY_STATE_ROOT="$TMP_ROOT/target-link" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "symlink destination was accepted" >&2
  exit 1
fi
test "$(stat -f%Lp "$OUTSIDE")" = 755

SYMLINK_INSTANCE="$TMP_ROOT/symlink-instance"
mkdir -p "$SYMLINK_INSTANCE/skills/earn/state" "$SYMLINK_INSTANCE/.blockrun"
ln -s "$OUTSIDE" "$SYMLINK_INSTANCE/identity"
printf '%s\n' outside-genesis > "$OUTSIDE/genesis.md"
if AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$SYMLINK_INSTANCE" AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" AGENT_ECONOMY_STATE_ROOT="$OWNER/.local/state/life-manager/source-link-fail" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "symlink source parent was accepted" >&2
  exit 1
fi

FAKE_BIN="$TMP_ROOT/fake-bin"
mkdir "$FAKE_BIN"
printf '%s\n' '#!/usr/bin/env bash' 'exit 9' > "$FAKE_BIN/cp"
chmod +x "$FAKE_BIN/cp"
COPY_FAIL="$OWNER/.local/state/life-manager/copy-fail"
if PATH="$FAKE_BIN:$PATH" AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" AGENT_ECONOMY_STATE_ROOT="$COPY_FAIL" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "failed copy reported success" >&2
  exit 1
fi
test "$(find "$COPY_FAIL" -type f 2>/dev/null | wc -l | tr -d ' ')" = 0

rm -f "$FAKE_BIN/cp"
printf '%s\n' '#!/usr/bin/env bash' 'exit 8' > "$FAKE_BIN/ln"
chmod +x "$FAKE_BIN/ln"
LINK_FAIL="$OWNER/.local/state/life-manager/link-fail"
if PATH="$FAKE_BIN:$PATH" AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" AGENT_ECONOMY_STATE_ROOT="$LINK_FAIL" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "failed publish reported success" >&2
  exit 1
fi
test "$(find "$LINK_FAIL" -type f 2>/dev/null | wc -l | tr -d ' ')" = 0

if AGENT_ECONOMY_LEGACY_INSTANCE_HOME="$INSTANCE" AGENT_ECONOMY_LEGACY_OWNER_HOME="$OWNER" AGENT_ECONOMY_STATE_ROOT="$OUTSIDE/child/.." \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "unsafe parent component was accepted" >&2
  exit 1
fi
test "$(stat -f%Lp "$OUTSIDE")" = 755
