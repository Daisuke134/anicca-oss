#!/usr/bin/env bash
set -euo pipefail

TMP_ROOT="$(mktemp -d)"
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

AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$NEW" \
  "$(dirname "$0")/migrate-legacy-state.sh"

test -f "$OLD/state/agentmail.db"
test "$(sqlite3 "$NEW/state/agentmail.db" 'select count(*) from messages;')" = 1
cmp "$OLD/state/inbox-queue.jsonl" "$NEW/state/inbox-queue.jsonl"
cmp "$OLD/state/agentmail-adapter/sent-log.jsonl" "$NEW/state/adapter/sent-log.jsonl"
cmp "$OLD/state/agentmail-semantic/result.json" "$NEW/state/semantic/result.json"
cmp "$OLD/logs/agentmail-replier.log" "$NEW/logs/agentmail-replier.log"

printf '%s\n' 'keep-target' > "$NEW/state/inbox-queue.jsonl"
AGENTMAIL_LEGACY_ROOT="$OLD" AGENTMAIL_STATE_ROOT="$NEW" \
  "$(dirname "$0")/migrate-legacy-state.sh"
test "$(cat "$NEW/state/inbox-queue.jsonl")" = keep-target
