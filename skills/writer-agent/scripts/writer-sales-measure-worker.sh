#!/usr/bin/env bash
# Hourly receipt collector: external dashboards -> observations -> canonical ledger.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"
LOCK_DIR="$STATE_DIR/.sales-measure.lock"
LOCK_PID="$LOCK_DIR/pid"
CLOAK_PYTHON="${WRITER_BROWSER_PYTHON:-$(command -v python3)}"

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" >"$LOCK_PID"
    return 0
  fi
  local owner=""
  owner="$(sed -n '1p' "$LOCK_PID" 2>/dev/null || true)"
  if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
    printf 'sales measurement already owned by pid=%s\n' "$owner"
    return 1
  fi
  rmdir "$LOCK_DIR" 2>/dev/null || return 1
  mkdir "$LOCK_DIR"
  printf '%s\n' "$$" >"$LOCK_PID"
}

acquire_lock || exit 0
trap 'rm -f -- "$LOCK_PID"; rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

[ -x "$CLOAK_PYTHON" ] || {
  printf 'sales measurement unavailable: cloak runtime missing\n' >&2
  exit 75
}

"$CLOAK_PYTHON" "$SCRIPT_DIR/measure-sales.py" \
  --out "$STATE_DIR/sales-ledger.jsonl"
python3 "$SCRIPT_DIR/money_sync.py" \
  --state-dir "$STATE_DIR" --db "$STATE_DIR/money.sqlite3"
