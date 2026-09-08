#!/usr/bin/env bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"  # launchd has a minimal PATH; python3 lives in homebrew
# DEPRECATED (self-heal-allslots sprint, FIND-006): this single-slot, sol-trade-hardcoded script is
# SUPERSEDED by the registry-driven generalization skills/self/earning-health-allslots.sh (which
# checks earn/sol-trade -- AND every other required earn slot -- via ONE shared script + one
# skills/self/earning-health-registry.json entry, reusing this file's exact detection algorithm
# unmodified via earning-health.py::is_fresh_but_barren). Kept in place, NOT deleted: the
# pre-existing ai.anicca.sol-trade-earning-healthcheck.plist may still reference this file until
# that plist is unloaded and migrated to ai.anicca.earning-health-allslots (see
# skills/self/launchd/README.md's explicit unload-before-load instruction; NEITHER plist is
# `launchctl load`ed by this sprint). Do not add new features here -- extend
# earning-health-allslots.sh + the registry instead; remove this file once the old plist is
# retired.
#
# sol-trade-healthcheck.sh — CONTENT check for the earn/sol-trade slot, NOT an artifact-age check.
# THE BLIND SPOT THIS CLOSES (Franklin, 2026-07-08 -> 2026-07-10, ~46h): sol-trade.trace.jsonl got a
# new line on EVERY wake (a fresh "skip" line, roughly hourly), so the existing artifact-AGE
# staleness check (healthcheck-lib.sh's OUT_STALE_HRS, FIND-009 step (d)) never fired -- the file
# kept looking "fresh" the entire time. The actual bug (a path resolution regression in run.sh made
# the identity-match guard reject every wake) only shows up if you read what the trace SAYS, not how
# recently it was touched. This script reads the content instead: if the last MIN_RUN wakes are ALL
# action=skip with the identical reason, the MECHANISM itself is broken (not the agent's trading
# judgment -- see earning-health.py's docstring) -> escalate to self-fix, same give-up path
# healthcheck-lib.sh's hc_run() step (d) and cadence-deadline-check.sh already use.
#
# Instance-agnostic by design: every path below is resolved RELATIVE TO THIS SCRIPT'S OWN location
# (mirrors run.sh's own $SKILL_DIR-relative STATE_DIR), so whichever copy of the repo this runs from
# (a dev checkout, or an ANICCA_HOME-rsynced deployment such as Franklin's ~/.blockrun) it checks ITS
# OWN adjacent trace/state -- never a different instance's absolute path baked into shared OSS code.
set -uo pipefail
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF_DIR="${SOL_TRADE_HC_SELF_DIR:-$SKILL_DIR/../../self}"
STATE_ROOT="${EARN_STATE_ROOT:-${LIFE_MANAGER_SKILLS_STATE_ROOT:-${ANICCA_HOME:-$HOME/.local/state/life-manager}/state/skills}/earn}"
TRACE="${SOL_TRADE_HC_TRACE:-$STATE_ROOT/sol-trade.trace.jsonl}"
MIN_RUN="${SOL_TRADE_HC_MIN_RUN:-20}"
STATE_DIR="${SOL_TRADE_HC_STATE_DIR:-$HOME/.local/state/life-manager/state}"; mkdir -p "$STATE_DIR" 2>/dev/null || true
LOG="${SOL_TRADE_HC_LOG:-$HOME/.local/state/life-manager/logs/sol-trade-healthcheck.log}"; mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
ESCALATE_EVERY_HRS="${SOL_TRADE_HC_ESCALATE_EVERY_HRS:-24}"

if [ ! -f "$TRACE" ]; then
  echo "$(date '+%F %T') no trace file at $TRACE -- nothing to check (not deployed/run here yet)" >> "$LOG"
  exit 0
fi

# bound the read (trace files grow forever); MIN_RUN*3 is always >= MIN_RUN with headroom for a mix
# of live-pass/skip/sol-gate lines in between, without reading a multi-hundred-KB file every 5min.
TAIL_JSON="$(tail -n "$(( MIN_RUN * 3 ))" "$TRACE" 2>/dev/null)"

if printf '%s\n' "$TAIL_JSON" | python3 "$SELF_DIR/earning-health.py" is-barren "$MIN_RUN"; then
  REASON="$(printf '%s\n' "$TAIL_JSON" | tail -1 | python3 -c '
import json, sys
try:
    print(json.loads(sys.stdin.read()).get("reason", "unknown"))
except Exception:
    print("unknown")
' 2>/dev/null)"
  # reason=="kill-switch" is run.sh's own explicit, operator-authored freeze (a literal KILL file
  # dropped next to run.sh, checked at run.sh's own top -- see its "OWN Solana wallet ... = kill-
  # switch" comment). That is NOT a bug to diagnose: it is the loop being off on purpose. Escalating
  # it to self-fix every ESCALATE_EVERY_HRS wastes a full self-fix agent spawn re-discovering an
  # intentional freeze forever (SOL-1, 2026-07-17: frozen after 850 passes / 0 swaps / $0 realized).
  if [ "$REASON" = "kill-switch" ]; then
    echo "$(date '+%F %T') FROZEN (intentional KILL file present, reason=kill-switch) -- not a bug, no escalation" >> "$LOG"
    exit 0
  fi
  now=$(date +%s)
  MK="$STATE_DIR/.sol-trade-earning-health-escalated"
  age_hrs=99999
  [ -f "$MK" ] && age_hrs=$(( (now - $(stat -f %m "$MK" 2>/dev/null || echo 0)) / 3600 ))
  if [ "$age_hrs" -ge "$ESCALATE_EVERY_HRS" ]; then
    touch "$MK"
    echo "$(date '+%F %T') BARREN: last $MIN_RUN trace lines are all skip/'$REASON' -> self-fix escalated" >> "$LOG"
    bash "$SELF_DIR/self-fix.sh" "sol-trade" "earn/sol-trade trace is fresh (a new line every wake) but the last $MIN_RUN wakes are ALL action=skip with the identical reason '$REASON' -- the loop is alive but a deterministic guard (identity-mismatch / kill-switch / earn-guard) is rejecting every wake before the agent ever gets to trade. Diagnose why (identity resolution, ANICCA_HOME/ANICCA_REPO path, env) and fix it so the agent actually gets to run its pass." >> "$LOG" 2>&1 \
      || echo "$(date '+%F %T') self-fix launch failed" >> "$LOG"
  else
    echo "$(date '+%F %T') BARREN but already escalated ${age_hrs}h ago (<${ESCALATE_EVERY_HRS}h) -- skip (no repeat spam)" >> "$LOG"
  fi
else
  echo "$(date '+%F %T') OK (last $MIN_RUN trace entries are not all-same-reason-skip)" >> "$LOG"
fi
