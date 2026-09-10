#!/usr/bin/env bash
# anicca-life-manager 5-min heartbeat entrypoint.
# Deterministic: gcal departBy x Telegram Live Location -> call if late-risk.
set -uo pipefail
# NOTE: the hard quiet-hours shell guard was removed 2026-06-09. Quiet-hours
# logic now lives INSIDE lateness_check.py (event-aware): routine polling stays
# silent during the quiet window, but an imminent wake / meditation / meds /
# sleep event punches through (Dais: "they are not calling me when i wake up").
SKILL="$LIFE_MANAGER_REPO/skills/anicca-life-manager"
PYTHON_BIN="${LIFE_MANAGER_PYTHON:-python3}"
LIFE_MANAGER_HOME="${LIFE_MANAGER_HOME:-$HOME/.local/state/life-manager}"
STATE_ROOT="${LIFE_MANAGER_STATE_ROOT:-$LIFE_MANAGER_HOME/lateness-heartbeat}"
MIGRATION_MARKER="$STATE_ROOT/legacy-migration.json"
if [ ! -f "$MIGRATION_MARKER" ]; then
  "$PYTHON_BIN" "$LIFE_MANAGER_REPO/runtime/migrate-legacy-lateness-state.py" \
    --target-home "$LIFE_MANAGER_HOME" --target-loop-root "$STATE_ROOT" || exit $?
fi
LOG="$STATE_ROOT/logs/run.log"
mkdir -p "$(dirname "$LOG")"
chmod 700 "$STATE_ROOT" "$(dirname "$LOG")"
unset ANICCA_HOME OPENCLAW_ENV_FILE
export LIFE_MANAGER_HOME
export LIFE_MANAGER_ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$LIFE_MANAGER_HOME/.env}"
# Load env: GOOGLE_API_KEY, TWILIO_*, GOG_*
set -a; source "$LIFE_MANAGER_ENV_FILE" 2>/dev/null; set +a
unset ANICCA_HOME OPENCLAW_ENV_FILE
echo "=== lateness run $(date '+%Y-%m-%d %H:%M:%S %Z') ===" >> "$LOG"
"$PYTHON_BIN" "$LIFE_MANAGER_REPO/runtime/run-with-timeout.py" --grace-seconds 10 110 "$PYTHON_BIN" \
  "$SKILL/scripts/lateness_check.py" "$@" >> "$LOG" 2>&1
LATENESS_STATUS=$?
echo "exit=$LATENESS_STATUS" >> "$LOG"

# === arrival closure (merged from anicca-arrival-mail v7.6) ===
"$PYTHON_BIN" "$LIFE_MANAGER_REPO/runtime/run-with-timeout.py" 60 "$PYTHON_BIN" \
  "$SKILL/scripts/arrival.py" >> "$LOG" 2>&1 || true
exit "$LATENESS_STATUS"
