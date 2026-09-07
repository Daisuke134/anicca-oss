#!/usr/bin/env bash
# Copy Reddit history out of legacy checkout/OpenClaw roots before registry cutover.
# Sources remain untouched and destination-owned files are never overwritten.
set -euo pipefail

LEGACY_SEGMENT=".open""claw"
LEGACY_WORK_STATE="${REDDIT_LEGACY_WORK_STATE:-}"
LEGACY_RUNTIME_STATE="${REDDIT_LEGACY_RUNTIME_STATE:-$HOME/$LEGACY_SEGMENT/state}"
LEGACY_LOG_ROOT="${REDDIT_LEGACY_LOG_ROOT:-$HOME/$LEGACY_SEGMENT/logs}"
DEST_ROOT="${REDDIT_STATE_ROOT:-$HOME/.local/state/life-manager/reddit}"

file_size() {
  stat -f%z "$1" 2>/dev/null || stat -c%s "$1"
}

copied=0
skipped=0
verified=0

copy_file() {
  local source="$1" target="$2"
  [ -f "$source" ] || return 0
  mkdir -p "$(dirname "$target")"
  if [ -e "$target" ]; then
    skipped=$((skipped + 1))
  else
    cp -p "$source" "$target"
    copied=$((copied + 1))
  fi
  if [ "$(file_size "$target")" -lt "$(file_size "$source")" ]; then
    printf 'destination is stale or partial: %s is smaller than %s\n' "$target" "$source" >&2
    exit 1
  fi
  verified=$((verified + 1))
}

copy_tree() {
  local source_root="$1" target_root="$2"
  [ -d "$source_root" ] || return 0
  while IFS= read -r -d '' source; do
    copy_file "$source" "$target_root/${source#"$source_root"/}"
  done < <(find "$source_root" -type f ! -name '.gitignore' -print0)
}

copy_tree "$LEGACY_WORK_STATE" "$DEST_ROOT/state"
for marker in .reddit-loop-last-pass reddit-loop-selfheal-request.json; do
  copy_file "$LEGACY_RUNTIME_STATE/$marker" "$DEST_ROOT/state/$marker"
done
copy_tree "$LEGACY_RUNTIME_STATE/agent-runner-evidence/reddit-daily" \
  "$DEST_ROOT/state/agent-runner-evidence/reddit-daily"

for name in reddit-loop-daily.log reddit-loop-daily.out reddit-loop-daily.err \
  reddit-loop-healthcheck.log reddit-loop-launchd.out.log reddit-loop-launchd.err.log; do
  copy_file "$LEGACY_LOG_ROOT/$name" "$DEST_ROOT/logs/$name"
done

printf 'reddit migration: copied=%s skipped=%s verified=%s destination=%s; sources untouched\n' \
  "$copied" "$skipped" "$verified" "$DEST_ROOT"
