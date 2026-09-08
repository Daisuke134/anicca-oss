#!/usr/bin/env bash
set -euo pipefail

TMP_ROOT="$(mktemp -d /private/tmp/lm-agentmail-migration.XXXXXX)"
trap 'rm -rf "$TMP_ROOT"' EXIT
OLD="$TMP_ROOT/old"
NEW="$TMP_ROOT/new"
mkdir -p "$OLD/state/agentmail-adapter" "$OLD/state/agentmail-semantic" "$OLD/logs"
printf '%s\n' '{"event":"mail"}' > "$OLD/state/inbox-queue.jsonl"
printf '%s\n' '1' > "$OLD/state/inbox-queue.jsonl.cursor"
printf '%s\n' 'sent' > "$OLD/state/agentmail-adapter/sent-log.jsonl"
printf '%s\n' 'evidence' > "$OLD/state/agentmail-semantic/result.json"
printf '%s\n' 'log' > "$OLD/logs/agentmail-replier.log"
sqlite3 "$OLD/state/agentmail.db" 'create table messages(id integer primary key); insert into messages values(1);'
chmod 640 "$OLD/state/agentmail.db"
SOURCE_DB_HASH_BEFORE="$(shasum -a 256 "$OLD/state/agentmail.db")"
SOURCE_DB_MODE_BEFORE="$(stat -f%Lp "$OLD/state/agentmail.db")"
SOURCE_DB_FILES_BEFORE="$(find "$OLD/state" -maxdepth 1 -name 'agentmail.db*' -print | sort)"

AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$NEW" \
  "$(dirname "$0")/migrate-legacy-state.sh"

test -f "$OLD/state/agentmail.db"
test "$(sqlite3 "$NEW/state/agentmail.db" 'select count(*) from messages;')" = 1
cmp "$OLD/state/inbox-queue.jsonl" "$NEW/state/inbox-queue.jsonl"
cmp "$OLD/state/agentmail-adapter/sent-log.jsonl" "$NEW/state/adapter/sent-log.jsonl"
cmp "$OLD/state/agentmail-semantic/result.json" "$NEW/state/semantic/result.json"
cmp "$OLD/logs/agentmail-replier.log" "$NEW/logs/agentmail-replier.log"
test "$(shasum -a 256 "$OLD/state/agentmail.db")" = "$SOURCE_DB_HASH_BEFORE"
test "$(stat -f%Lp "$OLD/state/agentmail.db")" = "$SOURCE_DB_MODE_BEFORE"
test "$(find "$OLD/state" -maxdepth 1 -name 'agentmail.db*' -print | sort)" = "$SOURCE_DB_FILES_BEFORE"
test "$(stat -f%Lp "$NEW")" = 700
test "$(stat -f%Lp "$NEW/state/inbox-queue.jsonl")" = 600
test "$(find "$NEW" -type l | wc -l | tr -d ' ')" = 0

printf '%s\n' 'keep-target' > "$NEW/state/inbox-queue.jsonl"
AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$NEW" \
  "$(dirname "$0")/migrate-legacy-state.sh"
test "$(cat "$NEW/state/inbox-queue.jsonl")" = keep-target
chmod 644 "$NEW/state/inbox-queue.jsonl" "$NEW/state/agentmail.db"
AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$NEW" \
  "$(dirname "$0")/migrate-legacy-state.sh"
test "$(stat -f%Lp "$NEW/state/inbox-queue.jsonl")" = 600
test "$(stat -f%Lp "$NEW/state/agentmail.db")" = 600

FAIL_NEW="$TMP_ROOT/fail-new"
mkdir -p "$FAIL_NEW/state"
FAKE_BIN="$TMP_ROOT/fake-bin"
mkdir "$FAKE_BIN"
printf '%s\n' '#!/usr/bin/env bash' 'exit 9' > "$FAKE_BIN/python3"
chmod +x "$FAKE_BIN/python3"
if PATH="$FAKE_BIN:$PATH" AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$FAIL_NEW" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "failed database backup reported success" >&2
  exit 1
fi
test ! -e "$FAIL_NEW/state/agentmail.db"
rm -f "$FAKE_BIN/python3"

LINK_FAIL_NEW="$TMP_ROOT/link-fail-new"
mkdir -p "$LINK_FAIL_NEW/state"
printf '%s\n' '#!/usr/bin/env bash' 'exit 8' > "$FAKE_BIN/ln"
chmod +x "$FAKE_BIN/ln"
if PATH="$FAKE_BIN:$PATH" AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$LINK_FAIL_NEW" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "failed database publish reported success" >&2
  exit 1
fi
test ! -e "$LINK_FAIL_NEW/state/agentmail.db"
test "$(find "$LINK_FAIL_NEW/state" -name '.agentmail.db.*' | wc -l | tr -d ' ')" = 0

if AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$OLD" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "overlapping destination was accepted" >&2
  exit 1
fi

OUTSIDE="$TMP_ROOT/outside"
mkdir -m 755 "$OUTSIDE"
ln -s "$OUTSIDE" "$TMP_ROOT/new-link"
if AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$TMP_ROOT/new-link" \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "symlink destination was accepted" >&2
  exit 1
fi
test "$(stat -f%Lp "$OUTSIDE")" = 755

if AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$OUTSIDE/child/.." \
  "$(dirname "$0")/migrate-legacy-state.sh" >/dev/null 2>&1; then
  echo "unsafe parent component was accepted" >&2
  exit 1
fi
test "$(stat -f%Lp "$OUTSIDE")" = 755
