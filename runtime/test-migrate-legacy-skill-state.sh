#!/usr/bin/env bash
set -euo pipefail

TEST_TEMP_ROOT="${TMPDIR:-/tmp}"
TEST_TEMP_ROOT="${TEST_TEMP_ROOT%/}"
TEST_TEMP_ROOT="$(cd "$TEST_TEMP_ROOT" && pwd -P)"
TMP_ROOT="$(mktemp -d "$TEST_TEMP_ROOT/lm-skill-state-migration.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT
MIGRATION_TMP="$TMP_ROOT/manifest-temp"
mkdir "$MIGRATION_TMP"
export TMPDIR="$MIGRATION_TMP"
mode_of() {
  stat -f%Lp "$1" 2>/dev/null || stat -c %a "$1"
}
INSTANCE="$TMP_ROOT/instance"
TARGET="$INSTANCE/state/skills"
mkdir -p "$INSTANCE/skills/earn/state/nested" "$INSTANCE/skills/economy/gig/state"
printf '%s\n' ledger > "$INSTANCE/skills/earn/state/earn-ledger.jsonl"
printf '%s\n' nested > "$INSTANCE/skills/earn/state/nested/evidence.json"
printf '%s\n' seen > "$INSTANCE/skills/economy/gig/state/seen.json"
SOURCE_HASH_BEFORE="$(shasum -a 256 "$INSTANCE/skills/earn/state/earn-ledger.jsonl")"
SOURCE_MODE_BEFORE="$(mode_of "$INSTANCE/skills/earn/state/earn-ledger.jsonl")"

ANICCA_HOME="$INSTANCE" "$(dirname "$0")/migrate-legacy-skill-state.sh"
cmp "$INSTANCE/skills/earn/state/earn-ledger.jsonl" "$TARGET/earn/earn-ledger.jsonl"
cmp "$INSTANCE/skills/earn/state/nested/evidence.json" "$TARGET/earn/nested/evidence.json"
cmp "$INSTANCE/skills/economy/gig/state/seen.json" "$TARGET/economy/gig/seen.json"
test -f "$INSTANCE/skills/earn/state/earn-ledger.jsonl"
test "$(shasum -a 256 "$INSTANCE/skills/earn/state/earn-ledger.jsonl")" = "$SOURCE_HASH_BEFORE"
test "$(mode_of "$INSTANCE/skills/earn/state/earn-ledger.jsonl")" = "$SOURCE_MODE_BEFORE"
test "$(mode_of "$TARGET")" = 700
test "$(mode_of "$TARGET/earn/earn-ledger.jsonl")" = 600

printf '%s\n' keep-target > "$TARGET/earn/earn-ledger.jsonl"
ANICCA_HOME="$INSTANCE" "$(dirname "$0")/migrate-legacy-skill-state.sh"
test "$(cat "$TARGET/earn/earn-ledger.jsonl")" = keep-target
test "$(mode_of "$TARGET/earn/earn-ledger.jsonl")" = 600

if ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$INSTANCE/skills" \
  "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
  echo "overlapping destination was accepted" >&2
  exit 1
fi

OUTSIDE="$TMP_ROOT/outside"
mkdir -m 755 "$OUTSIDE"
ln -s "$OUTSIDE" "$TMP_ROOT/target-link"
if ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$TMP_ROOT/target-link" \
  "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
  echo "symlink destination was accepted" >&2
  exit 1
fi
test "$(mode_of "$OUTSIDE")" = 755

if TMPDIR="$INSTANCE/skills/earn/state" ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$INSTANCE/state/tmp-inside-source" \
  "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
  echo "source-overlapping TMPDIR was accepted" >&2
  exit 1
fi
test ! -e "$INSTANCE/state/tmp-inside-source"

ln -s "$INSTANCE/skills/earn/state" "$TMP_ROOT/source-temp-link"
if TMPDIR="$TMP_ROOT/source-temp-link" ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$INSTANCE/state/tmp-symlink-source" \
  "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
  echo "source-overlapping symlink TMPDIR was accepted" >&2
  exit 1
fi
test ! -e "$INSTANCE/state/tmp-symlink-source"

TARGET_TEMP="$INSTANCE/state/tmp-target"
mkdir -p "$TARGET_TEMP"
if TMPDIR="$TARGET_TEMP" ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$TARGET_TEMP/skills" \
  "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
  echo "destination-overlapping TMPDIR was accepted" >&2
  exit 1
fi
test ! -e "$TARGET_TEMP/skills"

if ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$INSTANCE/state" \
  "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
  echo "broad instance state destination was accepted" >&2
  exit 1
fi

FAKE_BIN="$TMP_ROOT/fake-bin"
mkdir "$FAKE_BIN"
for tool in find cp ln; do
  printf '%s\n' '#!/usr/bin/env bash' 'exit 9' > "$FAKE_BIN/$tool"
  chmod +x "$FAKE_BIN/$tool"
  FAILURE_TARGET="$INSTANCE/state/failure-$tool"
  if PATH="$FAKE_BIN:$PATH" ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$FAILURE_TARGET" \
    "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
    echo "$tool failure reported success" >&2
    exit 1
  fi
  test "$(find "$FAILURE_TARGET" -name '.migration.*' 2>/dev/null | wc -l | tr -d ' ')" = 0
  rm -f "$FAKE_BIN/$tool"
done

EXISTING_SCAN_TARGET="$INSTANCE/state/existing-scan-failure"
mkdir -p "$EXISTING_SCAN_TARGET"
printf '%s\n' '#!/usr/bin/env bash' 'exit 9' > "$FAKE_BIN/find"
chmod +x "$FAKE_BIN/find"
if PATH="$FAKE_BIN:$PATH" ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$EXISTING_SCAN_TARGET" \
  "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
  echo "destination symlink scan failure reported success" >&2
  exit 1
fi
rm -f "$FAKE_BIN/find"

printf '%s\n' '#!/usr/bin/env bash' 'case "$*" in *\.migration.*) exit 9;; esac' 'exec /bin/chmod "$@"' > "$FAKE_BIN/chmod"
chmod +x "$FAKE_BIN/chmod"
CHMOD_FAIL="$INSTANCE/state/failure-chmod"
if PATH="$FAKE_BIN:$PATH" ANICCA_HOME="$INSTANCE" LIFE_MANAGER_SKILLS_STATE_ROOT="$CHMOD_FAIL" \
  "$(dirname "$0")/migrate-legacy-skill-state.sh" >/dev/null 2>&1; then
  echo "chmod failure reported success" >&2
  exit 1
fi
test "$(find "$CHMOD_FAIL" -name '.migration.*' 2>/dev/null | wc -l | tr -d ' ')" = 0
