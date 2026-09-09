#!/usr/bin/env bash
# record-cost-event.sh — REQ-CEO-006: append ONE cost-event row for a single pass to
# $CEO_STATE_DIR/ledgers/cost-events.jsonl (defaults to the canonical Life Manager
# CEO runtime state when CEO_STATE_DIR is unset).
# Vendored in-repo replacement for the anicca repo founder-loop ceo record-cost-event script (REQ-CEO-012 "vendored" fix).
#
#   bash record-cost-event.sh <loop> <usd_estimate>
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/opt/homebrew/bin/python3; [ -x "$PY" ] || PY=python3
if [ -n "${LIFE_MANAGER_RELEASE_SHA:-}" ]; then
  BASE="${LIFE_MANAGER_CEO_STATE_ROOT:-$HOME/.local/state/life-manager/ceo-runner}"
else
  BASE="${CEO_STATE_DIR:-${LIFE_MANAGER_CEO_STATE_ROOT:-$HOME/.local/state/life-manager/ceo-runner}}"
fi
export CEO_CONFIG_ROOT="$HERE"

LOOP="${1:-}"
USD="${2:-}"

if [ -z "$LOOP" ] || [ -z "$USD" ]; then
  echo "usage: record-cost-event.sh <loop> <usd_estimate>" >&2
  exit 1
fi

if ! "$PY" -c "import sys; float(sys.argv[1])" "$USD" 2>/dev/null; then
  echo "record-cost-event: usd_estimate '$USD' is not numeric" >&2
  exit 1
fi

"$PY" "$HERE/bin/record_cost_event.py" "$BASE" "$LOOP" "$USD"
