#!/bin/bash
# anicca-life-manager 5-min heartbeat entrypoint.
# Deterministic: gcal departBy x Telegram Live Location -> call if late-risk.
# Invoked by openclaw cron b2bf06ee (dais-lateness-heartbeat) every 5 min.
set -uo pipefail
# NOTE: the hard quiet-hours shell guard was removed 2026-06-09. Quiet-hours
# logic now lives INSIDE lateness_check.py (event-aware): routine polling stays
# silent during the quiet window, but an imminent wake / meditation / meds /
# sleep event punches through (Dais: "they are not calling me when i wake up").
SKILL="$LIFE_MANAGER_REPO/skills/anicca-life-manager"
LOG="$SKILL/state/run.log"
mkdir -p "$SKILL/state"
# Load env: GOOGLE_API_KEY, TWILIO_*, GOG_*
set -a; source "$HOME/.local/state/life-manager/.env" 2>/dev/null; set +a
echo "=== lateness run $(date '+%Y-%m-%d %H:%M:%S %Z') ===" >> "$LOG"
/opt/homebrew/bin/timeout --kill-after=10 110 /opt/homebrew/bin/python3 \
  "$SKILL/scripts/lateness_check.py" "$@" >> "$LOG" 2>&1
echo "exit=$?" >> "$LOG"

# === arrival closure (merged from anicca-arrival-mail v7.6) ===
/opt/homebrew/bin/timeout 60 /opt/homebrew/bin/python3 \
  "$SKILL/scripts/arrival.py" >> "$LOG" 2>&1 || true
