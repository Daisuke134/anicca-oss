#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${AGENTMAIL_LEGACY_ROOT:-$HOME/.openclaw}"
TARGET_ROOT="${AGENTMAIL_STATE_ROOT:-$HOME/.local/state/life-manager/agentmail}"
SOURCE_STATE="$SOURCE_ROOT/state"
TARGET_STATE="$TARGET_ROOT/state"

mkdir -p "$TARGET_STATE" "$TARGET_ROOT/logs"

copy_file() {
  local source="$1" target="$2"
  [ -f "$source" ] || return 0
  [ ! -e "$target" ] || return 0
  cp -p "$source" "$target"
}

copy_tree() {
  local source="$1" target="$2"
  [ -d "$source" ] || return 0
  [ ! -e "$target" ] || return 0
  cp -Rp "$source" "$target"
}

copy_file "$SOURCE_STATE/inbox-queue.jsonl" "$TARGET_STATE/inbox-queue.jsonl"
copy_file "$SOURCE_STATE/inbox-queue.jsonl.cursor" "$TARGET_STATE/inbox-queue.jsonl.cursor"

if [ -f "$SOURCE_STATE/agentmail.db" ] && [ ! -e "$TARGET_STATE/agentmail.db" ]; then
  sqlite3 "$SOURCE_STATE/agentmail.db" ".backup '$TARGET_STATE/agentmail.db'"
fi

copy_tree "$SOURCE_STATE/agentmail-adapter" "$TARGET_STATE/adapter"
copy_tree "$SOURCE_STATE/agentmail-semantic" "$TARGET_STATE/semantic"

for source in "$SOURCE_ROOT"/logs/agentmail-*.log; do
  [ -f "$source" ] || continue
  copy_file "$source" "$TARGET_ROOT/logs/$(basename "$source")"
done

echo "AgentMail legacy state copied to $TARGET_ROOT; source retained."
