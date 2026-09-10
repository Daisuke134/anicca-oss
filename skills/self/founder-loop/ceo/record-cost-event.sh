#!/usr/bin/env bash
# record-cost-event.sh — REQ-CEO-020: append ONE cost event for THIS pass to
# ~/.anicca-founder/state/ceo-cost-events.jsonl (or $CEO_STATE_DIR/ceo-cost-events.jsonl in tests).
# Called by each roster loop's OWN pass-end reporting hook (the same place that already calls
# loop-report.sh) -- never by the CEO WEEKLY pass itself.
#
#   bash record-cost-event.sh <loop> <usd_estimate>
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOOP="${1:?usage: record-cost-event.sh <loop> <usd_estimate>}"
USD_ESTIMATE="${2:?usage: record-cost-event.sh <loop> <usd_estimate>}"
STATE_DIR="${CEO_STATE_DIR:-${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/founder-loop-cadence}/state}"
mkdir -p "$STATE_DIR"
python3 "$HERE/record_cost_event_cli.py" "$LOOP" "$USD_ESTIMATE" "$STATE_DIR"
