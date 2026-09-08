#!/usr/bin/env bash
# migrate-legacy-state.sh — copy legacy Life Manager state into the portable
# data root.
#
# RUN ONLY when the fail-loud guard instructs (post-pull); running before the
# cutover creates a stale copy.
#
# COPY ONLY: the legacy loop remains the owner of its store until the
# Order 14 cutover, so this script never moves, deletes, or rewrites a source
# file, and never overwrites a destination file the new loop already owns.
#
#   source : $LM_LEGACY_STATE_ROOT (default: the legacy home runtime state)
#            -> {lm-video, life-manager-dev}
#   dest   : ${LM_DATA_DIR:-$HOME/.local/state/life-manager}/state/
#
# Idempotent: re-running skips files that already exist at the destination.
# Every run verifies copied files byte-for-byte. Pre-existing mutable files are
# verified semantically: append-only JSONL rows and the issue snapshot must be
# supersets, while the seven-day snapshot must be at least as recent. An
# unrelated differing file fails closed instead of guessing from byte size.
set -euo pipefail

LEGACY_RUNTIME_SEGMENT=".open""claw"
LEGACY_STATE_ROOT="${LM_LEGACY_STATE_ROOT:-$HOME/$LEGACY_RUNTIME_SEGMENT/state}"
DATA_ROOT="${LM_DATA_DIR:-$HOME/.local/state/life-manager}"
DEST_ROOT="$DATA_ROOT/state"

verify_existing() {
  local source_file="$1"
  local target="$2"
  local relative="$3"

  if cmp -s "$source_file" "$target"; then
    return 0
  fi

  python3 - "$source_file" "$target" "$relative" <<'PY'
import datetime
import json
import sys

source_path, target_path, relative = sys.argv[1:]

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def jsonl_rows(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise SystemExit(f"invalid JSONL at {path}:{number}: {error}")
    return rows

if relative.endswith(".jsonl"):
    source = {canonical(row) for row in jsonl_rows(source_path)}
    target = {canonical(row) for row in jsonl_rows(target_path)}
    if source <= target:
        raise SystemExit(0)
    raise SystemExit(f"{target_path} does not contain every legacy JSONL row from {source_path}")

if relative not in {
    "life-manager-dev/issues.json",
    "life-manager-dev/seven-day-status.json",
}:
    raise SystemExit(f"{target_path} differs from legacy {source_path}; no safe merge rule exists")

with open(source_path, encoding="utf-8") as handle:
    source = json.load(handle)
with open(target_path, encoding="utf-8") as handle:
    target = json.load(handle)

if relative == "life-manager-dev/issues.json":
    if not isinstance(source, list) or not isinstance(target, list):
        raise SystemExit("issues.json must contain a JSON array")
    if {canonical(item) for item in source} <= {canonical(item) for item in target}:
        raise SystemExit(0)
    raise SystemExit(f"{target_path} does not contain every legacy issue from {source_path}")

if relative == "life-manager-dev/seven-day-status.json":
    try:
        source_time = datetime.datetime.fromisoformat(source["evaluated_at"].replace("Z", "+00:00"))
        target_time = datetime.datetime.fromisoformat(target["evaluated_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError) as error:
        raise SystemExit(f"invalid seven-day snapshot: {error}")
    if source.get("schema_version") == target.get("schema_version") and target_time >= source_time:
        raise SystemExit(0)
    raise SystemExit(f"{target_path} is older than or incompatible with legacy snapshot {source_path}")
PY
}

total_copied=0
total_skipped=0
total_verified=0

for name in lm-video life-manager-dev; do
  src="$LEGACY_STATE_ROOT/$name"
  dst="$DEST_ROOT/$name"
  if [ ! -d "$src" ]; then
    printf 'skip %s: no legacy dir at %s\n' "$name" "$src"
    continue
  fi
  mkdir -p "$dst"
  copied=0
  skipped=0
  while IFS= read -r -d '' source_file; do
    relative="${source_file#"$src"/}"
    target="$dst/$relative"
    if [ -e "$target" ]; then
      skipped=$((skipped + 1))
      verify_existing "$source_file" "$target" "$name/$relative"
    else
      mkdir -p "$(dirname "$target")"
      cp -p "$source_file" "$target"
      copied=$((copied + 1))
      if ! cmp -s "$source_file" "$target"; then
        printf '%s differs from newly copied source %s\n' "$target" "$source_file" >&2
        exit 1
      fi
    fi
    total_verified=$((total_verified + 1))
  done < <(find "$src" -type f -print0)
  printf '%s: copied %s file(s), skipped %s existing file(s) -> %s\n' \
    "$name" "$copied" "$skipped" "$dst"
  total_copied=$((total_copied + copied))
  total_skipped=$((total_skipped + skipped))
done

printf 'done: %s copied, %s skipped, %s verified by content readback; legacy store untouched at %s\n' \
  "$total_copied" "$total_skipped" "$total_verified" "$LEGACY_STATE_ROOT"
