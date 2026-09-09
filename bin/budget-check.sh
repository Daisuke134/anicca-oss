#!/usr/bin/env bash
# budget-check.sh — REQ-CEO-013: compute this-month/this-week spend per loop against
# config/ceo-budget-config.json and enforce hard limits (pause + ceo-decisions.jsonl row) /
# soft limits (lessons.jsonl warning row). Callable scoped to one loop (--loop <name>, the exact
# signature lib/registry-enforce.sh invokes at dispatch time, REQ-CEO-009 step 0) or for every
# registry loop when called with no arguments.
#
#   bash budget-check.sh [--loop <name>]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/opt/homebrew/bin/python3; [ -x "$PY" ] || PY=python3
if [ -n "${LIFE_MANAGER_RELEASE_SHA:-}" ]; then
  BASE="${LIFE_MANAGER_CEO_STATE_ROOT:-$HOME/.local/state/life-manager/ceo-runner}"
else
  BASE="${CEO_STATE_DIR:-${LIFE_MANAGER_CEO_STATE_ROOT:-$HOME/.local/state/life-manager/ceo-runner}}"
fi
export CEO_CONFIG_ROOT="$HERE"

LOOP=""
if [ "${1:-}" = "--loop" ]; then
  LOOP="${2:-}"
fi

mkdir -p "$BASE/ledgers"
"$PY" "$HERE/bin/budget_check_cli.py" "$BASE" "$LOOP"
