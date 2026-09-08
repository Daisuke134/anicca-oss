#!/usr/bin/env bash
set -euo pipefail
umask 077

HOME_ROOT="${ANICCA_HOME:?ANICCA_HOME is required}"
SOURCE="$HOME_ROOT/skills"
TARGET="${LIFE_MANAGER_SKILLS_STATE_ROOT:-$HOME_ROOT/state/skills}"

fail_destination() {
  printf 'unsafe skill-state migration destination: %s\n' "$1" >&2
  exit 2
}

reject_symlink_components() {
  local component="$1"
  while [ "$component" != "/" ]; do
    [ ! -L "$component" ] || fail_destination "path contains a symlink"
    component="$(dirname "$component")"
  done
}

reject_tree_symlinks() {
  local root="$1" found
  [ -d "$root" ] || return 0
  found="$(find "$root" -type l -print -quit)"
  [ -z "$found" ] || fail_destination "existing tree contains a symlink"
}

physical_path() {
  local candidate="$1" suffix="" parent
  while [ ! -e "$candidate" ]; do
    parent="$(dirname "$candidate")"
    suffix="/$(basename "$candidate")$suffix"
    [ "$parent" != "$candidate" ] || fail_destination "path cannot be resolved"
    candidate="$parent"
  done
  [ -d "$candidate" ] || fail_destination "path ancestor is not a directory"
  printf '%s%s\n' "$(cd "$candidate" && pwd -P)" "$suffix"
}

reject_overlap() {
  local left="$1" right="$2" message="$3"
  case "$left/" in "$right/"*) fail_destination "$message" ;; esac
  case "$right/" in "$left/"*) fail_destination "$message" ;; esac
}

case "$HOME_ROOT" in /*) ;; *) fail_destination "ANICCA_HOME must be absolute" ;; esac
case "$TARGET" in /*) ;; *) fail_destination "path must be absolute" ;; esac
case "$HOME_ROOT/" in *"/../"*|*"/./"*|*"//"*) fail_destination "ANICCA_HOME contains an unsafe component" ;; esac
case "$TARGET/" in *"/../"*|*"/./"*|*"//"*) fail_destination "path contains an unsafe component" ;; esac
HOME_ROOT="${HOME_ROOT%/}"
SOURCE="$HOME_ROOT/skills"
TARGET="${TARGET%/}"
case "$TARGET" in /|"$HOME"|"$HOME/.local"|"$HOME/.local/state"|"$HOME/.local/state/life-manager"|"$HOME_ROOT/state") fail_destination "path is too broad" ;; esac
reject_symlink_components "$SOURCE"
reject_symlink_components "$TARGET"
case "$TARGET/" in "$SOURCE/"*) fail_destination "path overlaps the source" ;; esac
case "$SOURCE/" in "$TARGET/"*) fail_destination "path contains the source" ;; esac
[ ! -e "$TARGET" ] || [ -d "$TARGET" ] || fail_destination "existing path is not a directory"
reject_tree_symlinks "$TARGET"

[ -d "$SOURCE" ] || { echo "No legacy skill state found; nothing copied."; exit 0; }

STATE_MANIFEST=""
FILE_MANIFEST=""
ACTIVE_TEMP=""
cleanup() {
  [ -z "$ACTIVE_TEMP" ] || rm -f -- "$ACTIVE_TEMP"
  [ -z "$FILE_MANIFEST" ] || rm -f -- "$FILE_MANIFEST"
  [ -z "$STATE_MANIFEST" ] || rm -f -- "$STATE_MANIFEST"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

TASK_TEMP_ROOT="${TMPDIR:-/tmp}"
TASK_TEMP_ROOT="${TASK_TEMP_ROOT%/}"
[ -d "$TASK_TEMP_ROOT" ] && [ -w "$TASK_TEMP_ROOT" ] || fail_destination "secure temporary directory unavailable"
SOURCE_PHYSICAL="$(physical_path "$SOURCE")"
TARGET_PHYSICAL="$(physical_path "$TARGET")"
TASK_TEMP_ROOT_PHYSICAL="$(physical_path "$TASK_TEMP_ROOT")"
reject_overlap "$TASK_TEMP_ROOT_PHYSICAL" "$SOURCE_PHYSICAL" "temporary directory overlaps the source"
reject_overlap "$TASK_TEMP_ROOT_PHYSICAL" "$TARGET_PHYSICAL" "temporary directory overlaps the destination"
STATE_MANIFEST="$(mktemp "$TASK_TEMP_ROOT/lm-skill-state-dirs.XXXXXX")"
find "$SOURCE" -type d -name state -print0 > "$STATE_MANIFEST"

mkdir -p "$TARGET"
chmod 700 "$TARGET"

while IFS= read -r -d '' state_dir; do
  relative_parent="${state_dir#"$SOURCE"/}"
  relative_parent="${relative_parent%/state}"
  destination="$TARGET/$relative_parent"
  mkdir -p "$destination"
  chmod 700 "$destination"
  FILE_MANIFEST="$(mktemp "$TASK_TEMP_ROOT/lm-skill-state-files.XXXXXX")"
  find "$state_dir" -type f -print0 > "$FILE_MANIFEST"
  while IFS= read -r -d '' source_file; do
    relative_file="${source_file#"$state_dir"/}"
    target_file="$destination/$relative_file"
    mkdir -p "$(dirname "$target_file")"
    chmod 700 "$(dirname "$target_file")"
    if [ ! -e "$target_file" ]; then
      ACTIVE_TEMP="$(mktemp "$(dirname "$target_file")/.migration.XXXXXX")"
      if ! cp -p "$source_file" "$ACTIVE_TEMP" || ! cmp -s "$source_file" "$ACTIVE_TEMP"; then
        printf 'skill-state migration copy verification failed\n' >&2
        exit 1
      fi
      chmod 600 "$ACTIVE_TEMP"
      if ln "$ACTIVE_TEMP" "$target_file" 2>/dev/null; then
        rm -f -- "$ACTIVE_TEMP"
      elif [ -f "$target_file" ] && [ ! -L "$target_file" ]; then
        rm -f -- "$ACTIVE_TEMP"
      else
        fail_destination "file publish failed"
      fi
      ACTIVE_TEMP=""
    fi
    [ -f "$target_file" ] && [ ! -L "$target_file" ] || fail_destination "existing target is not a regular file"
    chmod 600 "$target_file"
  done < "$FILE_MANIFEST"
  rm -f -- "$FILE_MANIFEST"
  FILE_MANIFEST=""
done < "$STATE_MANIFEST"

find "$TARGET" -type d -exec chmod 700 {} +

echo "Legacy skill state copied to $TARGET; source retained."
