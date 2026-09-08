#!/usr/bin/env bash
set -euo pipefail
umask 077

SOURCE_ROOT="${AGENTMAIL_LEGACY_ROOT:-$HOME/.openclaw}"
TARGET_ROOT="${AGENTMAIL_STATE_ROOT:-$HOME/.local/state/life-manager/agentmail}"
SOURCE_STATE="$SOURCE_ROOT/state"
TARGET_STATE="$TARGET_ROOT/state"

fail_destination() {
  printf 'unsafe AgentMail migration destination: %s\n' "$1" >&2
  exit 2
}

reject_symlink_components() {
  local component="$1"
  while [ "$component" != "/" ]; do
    [ ! -L "$component" ] || fail_destination "path contains a symlink"
    component="$(dirname "$component")"
  done
}

validate_paths() {
  [ -n "$TARGET_ROOT" ] || fail_destination "empty path"
  case "$SOURCE_ROOT" in /*) ;; *) fail_destination "source path must be absolute" ;; esac
  case "$TARGET_ROOT" in /*) ;; *) fail_destination "path must be absolute" ;; esac
  case "$SOURCE_ROOT/" in *"/../"*|*"/./"*|*"//"*) fail_destination "source path contains an unsafe component" ;; esac
  case "$TARGET_ROOT/" in *"/../"*|*"/./"*|*"//"*) fail_destination "path contains an unsafe component" ;; esac
  SOURCE_ROOT="${SOURCE_ROOT%/}"
  TARGET_ROOT="${TARGET_ROOT%/}"
  SOURCE_STATE="$SOURCE_ROOT/state"
  TARGET_STATE="$TARGET_ROOT/state"
  case "$TARGET_ROOT" in
    /|"$HOME"|"$HOME/.local"|"$HOME/.local/state"|"$HOME/.local/state/life-manager")
      fail_destination "path is too broad"
      ;;
  esac
  reject_symlink_components "$SOURCE_ROOT"
  reject_symlink_components "$TARGET_ROOT"
  case "$TARGET_ROOT/" in "$SOURCE_ROOT/"*) fail_destination "path overlaps the source" ;; esac
  case "$SOURCE_ROOT/" in "$TARGET_ROOT/"*) fail_destination "path contains the source" ;; esac
  [ ! -e "$TARGET_ROOT" ] || [ -d "$TARGET_ROOT" ] || fail_destination "existing path is not a directory"
  if [ -d "$TARGET_ROOT" ] && find "$TARGET_ROOT" -type l -print -quit | grep -q .; then
    fail_destination "existing tree contains a symlink"
  fi
}

validate_paths
mkdir -p "$TARGET_STATE" "$TARGET_ROOT/logs"
chmod 700 "$TARGET_ROOT" "$TARGET_STATE" "$TARGET_ROOT/logs"

copy_file() {
  local source="$1" target="$2"
  [ -f "$source" ] || return 0
  mkdir -p "$(dirname "$target")"
  chmod 700 "$(dirname "$target")"
  if [ ! -e "$target" ]; then
    cp -p "$source" "$target"
  fi
  [ -f "$target" ] || fail_destination "existing target is not a regular file"
  chmod 600 "$target"
}

copy_tree() {
  local source="$1" target="$2"
  [ -d "$source" ] || return 0
  while IFS= read -r -d '' file; do
    copy_file "$file" "$target/${file#"$source"/}"
  done < <(find "$source" -type f -print0)
}

copy_file "$SOURCE_STATE/inbox-queue.jsonl" "$TARGET_STATE/inbox-queue.jsonl"
copy_file "$SOURCE_STATE/inbox-queue.jsonl.cursor" "$TARGET_STATE/inbox-queue.jsonl.cursor"

if [ -f "$SOURCE_STATE/agentmail.db" ]; then
  target_db="$TARGET_STATE/agentmail.db"
  if [ ! -e "$target_db" ]; then
    temp_db="$(mktemp "$TARGET_STATE/.agentmail.db.XXXXXX")"
    cleanup_temp_db() { rm -f -- "$temp_db"; }
    trap cleanup_temp_db EXIT
    python3 -c 'import pathlib, sqlite3, sys
source_uri = pathlib.Path(sys.argv[1]).resolve().as_uri() + "?mode=ro"
source = sqlite3.connect(source_uri, uri=True)
target = sqlite3.connect(sys.argv[2])
try:
    source.backup(target)
finally:
    target.close()
    source.close()
' "$SOURCE_STATE/agentmail.db" "$temp_db"
    [ "$(sqlite3 "$temp_db" 'pragma integrity_check;')" = "ok" ] || {
      printf 'AgentMail database backup failed integrity check\n' >&2
      exit 1
    }
    chmod 600 "$temp_db"
    if ln "$temp_db" "$target_db" 2>/dev/null; then
      rm -f -- "$temp_db"
      trap - EXIT
    elif [ -f "$target_db" ] && [ ! -L "$target_db" ]; then
      rm -f -- "$temp_db"
      trap - EXIT
    else
      fail_destination "database publish failed"
    fi
  fi
  [ -f "$target_db" ] || fail_destination "existing database target is not a regular file"
  chmod 600 "$target_db"
fi

copy_tree "$SOURCE_STATE/agentmail-adapter" "$TARGET_STATE/adapter"
copy_tree "$SOURCE_STATE/agentmail-semantic" "$TARGET_STATE/semantic"

for source in "$SOURCE_ROOT"/logs/agentmail-*.log; do
  [ -f "$source" ] || continue
  copy_file "$source" "$TARGET_ROOT/logs/$(basename "$source")"
done

find "$TARGET_ROOT" -type d -exec chmod 700 {} +

echo "AgentMail legacy state copied to $TARGET_ROOT; source retained."
