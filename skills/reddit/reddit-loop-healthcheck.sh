#!/usr/bin/env bash
# Reddit daily-driver healthcheck. Lifecycle repair belongs to lm-loop; this reports only.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
RUN_AGENT="${RUN_AGENT_BIN:-$REPO_ROOT/skills/earn/marketing-engine/run_agent.sh}"
if [ "${AGENT_WIRING_PROBE_ONLY:-0}" = "1" ]; then
  printf '{"task_class":"tool-agent","runner":"%s"}\n' "$RUN_AGENT"
  exit 0
fi

DAILY_LABEL="ai.anicca.hf-reddit-loop-daily"
STATE_ROOT="${REDDIT_STATE_ROOT:-$HOME/.local/state/life-manager/reddit}"
STATE="${REDDIT_STATE_DIR:-$STATE_ROOT/state}"
HB="$STATE/.reddit-loop-last-pass"
LOG="${REDDIT_HEALTHCHECK_LOG:-$STATE_ROOT/logs/reddit-loop-healthcheck.log}"
STALE_MIN=1560
LOCK_DIR="/tmp/.reddit-loop-healthcheck.lock"
mkdir -p "$(dirname "$LOG")" "$STATE"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then exit 0; fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

age_min=999999
if [ -f "$HB" ]; then
  age_min=$(( ($(date +%s) - $(stat -f %m "$HB")) / 60 ))
fi
if [ "$age_min" -lt "$STALE_MIN" ]; then
  echo "$(date '+%F %T') reddit daily heartbeat fresh (${age_min}min)" >> "$LOG"
  exit 0
fi

echo "$(date '+%F %T') reddit heartbeat stale/missing (${age_min}min); lifecycle repair required via lm-loop for $DAILY_LABEL" >> "$LOG"
exit 1
