#!/usr/bin/env bash
set -euo pipefail

HOME_ROOT="${ANICCA_HOME:?ANICCA_HOME is required}"
SOURCE="$HOME_ROOT/skills"
TARGET="${LIFE_MANAGER_SKILLS_STATE_ROOT:-$HOME_ROOT/state/skills}"

[ -d "$SOURCE" ] || { echo "No legacy skill state found; nothing copied."; exit 0; }
mkdir -p "$TARGET"

while IFS= read -r state_dir; do
  relative_parent="${state_dir#"$SOURCE"/}"
  relative_parent="${relative_parent%/state}"
  destination="$TARGET/$relative_parent"
  mkdir -p "$destination"
  while IFS= read -r source_file; do
    relative_file="${source_file#"$state_dir"/}"
    target_file="$destination/$relative_file"
    mkdir -p "$(dirname "$target_file")"
    [ -e "$target_file" ] || cp -p "$source_file" "$target_file"
  done < <(find "$state_dir" -type f -print)
done < <(find "$SOURCE" -type d -name state -print)

echo "Legacy skill state copied to $TARGET; source retained."
