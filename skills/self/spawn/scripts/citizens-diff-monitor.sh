#!/bin/bash
# citizens-diff-monitor — spawn Done-witness observer.
# Watches the self/spawn citizens registry for genuine new entries vs the seed baseline.
# READ-ONLY on citizens.json; writes only its own baseline/pid/log.
set -u

STATE_ROOT="${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/citizens-diff-monitor}"
SPAWN_STATE_ROOT="${ANICCA_STATE_DIR:-$HOME/.hermes/state}"
CITIZENS="${CITIZENS_REGISTRY_PATH:-$SPAWN_STATE_ROOT/citizens.json}"
BASELINE="$STATE_ROOT/citizens-baseline.json"
PIDFILE="$STATE_ROOT/citizens-diff-monitor.pid"
LOG="$STATE_ROOT/logs/citizens-diff-monitor.log"
INTERVAL="${CITIZENS_DIFF_INTERVAL:-60}"

mkdir -p "$STATE_ROOT/logs"
echo $$ > "$PIDFILE"
sleep_pid=""
cleanup() {
  if [ -n "$sleep_pid" ]; then
    kill "$sleep_pid" 2>/dev/null || true
    wait "$sleep_pid" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
}
trap cleanup EXIT
trap 'exit 0' INT TERM

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

if [ ! -f "$BASELINE" ]; then
  if [ -f "$CITIZENS" ]; then
    cp "$CITIZENS" "$BASELINE"
    echo "[$(ts)] baseline captured from current citizens.json ($(jq 'length' "$BASELINE" 2>/dev/null || echo '?') entries)" >> "$LOG"
  else
    echo "[$(ts)] WARN: citizens.json missing at startup; baseline deferred" >> "$LOG"
  fi
fi

echo "[$(ts)] monitor started pid=$$ interval=${INTERVAL}s watching $CITIZENS" >> "$LOG"

while true; do
  if [ -f "$CITIZENS" ]; then
    if [ ! -f "$BASELINE" ]; then
      cp "$CITIZENS" "$BASELINE"
      echo "[$(ts)] baseline captured late ($(jq 'length' "$BASELINE" 2>/dev/null || echo '?') entries)" >> "$LOG"
    elif ! cmp -s "$CITIZENS" "$BASELINE"; then
      base_ids=$(jq -r '[.[].id] | sort | join(",")' "$BASELINE" 2>/dev/null || echo "PARSE_ERROR")
      cur_ids=$(jq -r '[.[].id] | sort | join(",")' "$CITIZENS" 2>/dev/null || echo "PARSE_ERROR")
      {
        echo "[$(ts)] CHANGE DETECTED baseline_ids=[$base_ids] current_ids=[$cur_ids]"
        diff -u "$BASELINE" "$CITIZENS" || true
        if [ "$cur_ids" != "$base_ids" ] && [ "$cur_ids" != "PARSE_ERROR" ]; then
          echo "[$(ts)] *** NEW/REMOVED CITIZEN ENTRY — candidate spawn witness. Independent RPC verification of the new wallet is REQUIRED before treating this as Done evidence. ***"
        fi
      } >> "$LOG"
    fi
  else
    echo "[$(ts)] WARN: citizens.json missing" >> "$LOG"
  fi
  sleep "$INTERVAL" &
  sleep_pid=$!
  wait "$sleep_pid" || true
  sleep_pid=""
done
