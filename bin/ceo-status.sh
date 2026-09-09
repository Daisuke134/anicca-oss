#!/usr/bin/env bash
# ceo-status.sh — REQ-CEO-007: per-loop status/allocation/cadence/evidence/cost/revenue/budget
# report. Ensures the 4 CEO ledgers exist (REQ-CEO-005), then reads+renders. Always exits 0.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/opt/homebrew/bin/python3; [ -x "$PY" ] || PY=python3
if [ -n "${LIFE_MANAGER_RELEASE_SHA:-}" ]; then
  BASE="${LIFE_MANAGER_CEO_STATE_ROOT:-$HOME/.local/state/life-manager/ceo-runner}"
else
  BASE="${CEO_STATE_DIR:-${LIFE_MANAGER_CEO_STATE_ROOT:-$HOME/.local/state/life-manager/ceo-runner}}"
fi
export CEO_CONFIG_ROOT="$HERE"

"$PY" "$HERE/bin/ceo_status.py" "$BASE"
exit 0
