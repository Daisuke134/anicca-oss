#!/usr/bin/env bash
# Copy Bounty history out of legacy checkout/OpenClaw roots before the registry cutover.
# Sources remain untouched. Existing destination files are never overwritten.
set -euo pipefail
umask 077

LEGACY_SEGMENT=".open""claw"
# The former checkout location is deliberately not encoded in open-source runtime code.
# Set this only during the one-time host cutover when legacy business state exists.
LEGACY_WORK_STATE="${BOUNTY_LEGACY_WORK_STATE:-}"
LEGACY_RUNTIME_STATE="${BOUNTY_LEGACY_RUNTIME_STATE:-$HOME/$LEGACY_SEGMENT/state}"
LEGACY_LOG_ROOT="${BOUNTY_LEGACY_LOG_ROOT:-$HOME/$LEGACY_SEGMENT/logs}"
DEST_ROOT="${BOUNTY_STATE_ROOT:-$HOME/.local/state/life-manager/bounty}"

fail_destination() {
  printf 'unsafe bounty migration destination: %s\n' "$1" >&2
  exit 2
}

reject_symlink_components() {
  local component="$1"
  while [ "$component" != "/" ]; do
    [ ! -L "$component" ] || fail_destination "path contains a symlink"
    component="$(dirname "$component")"
  done
}

validate_destination() {
  local source
  [ -n "$DEST_ROOT" ] || fail_destination "empty path"
  case "$DEST_ROOT" in
    /*) ;;
    *) fail_destination "path must be absolute" ;;
  esac
  case "$DEST_ROOT/" in
    *"/../"*|*"/./"*|*"//"*) fail_destination "path contains an unsafe component" ;;
  esac
  DEST_ROOT="${DEST_ROOT%/}"
  [ -n "$DEST_ROOT" ] || DEST_ROOT="/"
  case "$DEST_ROOT" in
    /|"$HOME"|"$HOME/.local"|"$HOME/.local/state"|"$HOME/.local/state/life-manager")
      fail_destination "path is too broad"
      ;;
  esac
  for source in "$LEGACY_WORK_STATE" "$LEGACY_RUNTIME_STATE" "$LEGACY_LOG_ROOT"; do
    [ -n "$source" ] || continue
    case "$source" in /*) ;; *) fail_destination "source path must be absolute" ;; esac
    case "$source/" in
      *"/../"*|*"/./"*|*"//"*) fail_destination "source path contains an unsafe component" ;;
    esac
    source="${source%/}"
    reject_symlink_components "$source"
    case "$DEST_ROOT/" in "$source/"*) fail_destination "path overlaps a source" ;; esac
    case "$source/" in "$DEST_ROOT/"*) fail_destination "path contains a source" ;; esac
  done
  reject_symlink_components "$DEST_ROOT"
  [ ! -e "$DEST_ROOT" ] || [ -d "$DEST_ROOT" ] || fail_destination "existing path is not a directory"
  if [ -d "$DEST_ROOT" ] && find "$DEST_ROOT" -type l -print -quit | grep -q .; then
    fail_destination "existing tree contains a symlink"
  fi
}

validate_destination
mkdir -p "$DEST_ROOT"
chmod 700 "$DEST_ROOT"

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
  chmod 700 "$(dirname "$target")"
  if [ -e "$target" ]; then
    skipped=$((skipped + 1))
  else
    cp -p "$source" "$target"
    copied=$((copied + 1))
  fi
  chmod 600 "$target"
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
for marker in .bounty-core-last-start .bounty-core-last-pass .bounty-core-selfheal-request.json; do
  copy_file "$LEGACY_RUNTIME_STATE/$marker" "$DEST_ROOT/state/$marker"
done
copy_tree "$LEGACY_RUNTIME_STATE/agent-runner-evidence/bounty-daily" \
  "$DEST_ROOT/state/agent-runner-evidence/bounty-daily"

for name in bounty-daily.log bounty-daily.out.log bounty-daily.err.log \
  bounty-core-healthcheck.log bounty-core-launchd.out.log bounty-core-launchd.err.log; do
  copy_file "$LEGACY_LOG_ROOT/$name" "$DEST_ROOT/logs/$name"
done

find "$DEST_ROOT" -type d -exec chmod 700 {} +

printf 'bounty migration: copied=%s skipped=%s verified=%s destination=%s; sources untouched\n' \
  "$copied" "$skipped" "$verified" "$DEST_ROOT"
