#!/usr/bin/env bash
# daily_loop.sh — fire a provider-agnostic tool agent to drain ONE inventory listing to Capafy.
# Scheduled by launchd (com.anicca.capafy-daily). No human in the loop.
# Verification: lint (deterministic) + agent sanity re-read + split prepare/CP1/finish
# fail-closed gates, including official remote readback.
set -uo pipefail

AUTO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$AUTO" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
LIFE_MANAGER_STATE_HOME="${LIFE_MANAGER_STATE_HOME:-$HOME/.local/state/life-manager}"
CAPAFY_STATE_DIR="${CAPAFY_STATE_DIR:-$LIFE_MANAGER_STATE_HOME/state/capafy-autopublish}"
export LIFE_MANAGER_REPO LIFE_MANAGER_STATE_HOME CAPAFY_STATE_DIR
LOG="$CAPAFY_STATE_DIR/daily_loop.log"
RUN_AGENT="${CAPAFY_RUN_AGENT:-$LIFE_MANAGER_REPO/skills/earn/marketing-engine/run_agent.sh}"
# HEALTHY-PASS MARKER (self-fix-capafy-loop, 2026-07-08): touched whenever the loop reaches a
# HEALTHY terminal state — either it published something OR it correctly determined there is
# nothing to publish (inventory drained / cap full). The healthcheck watches THIS file's mtime,
# not published.jsonl. Why: this loop DRAINS a finite hand-built inventory; once every listing
# is online, published.jsonl can never grow, so the old "grew in 30h?" alarm false-escalated an
# Opus self-fix forever for a non-bug. This marker stays fresh while the loop is healthy-idle,
# and only goes stale if the loop stops running OR keeps hitting a real BLOCKED publish — which
# is exactly when a self-fix SHOULD fire.
MARK="$CAPAFY_STATE_DIR/.capafy-healthy-pass"
TS="$(date '+%Y-%m-%d %H:%M:%S')"
mkdir -p "$CAPAFY_STATE_DIR"

# EXCLUSIVE LOCK (self-fix-capafy-loop, 2026-07-12): two independent schedulers
# (launchd ai.anicca.capafy-loop-daily every hour via the full money-loop-core prompt,
# and OpenClaw cron "anicca-capafy-daily-publish" @ 09:00 JST calling this script directly)
# both end up invoking this drainer. Without a lock, overlapping runs raced on the SAME
# shared CloakBrowser tab (:9222), each seeing the other's mid-edit DOM state, burning
# through --max-turns without ever completing a save (observed 2026-07-12: 5 stacked
# invocations in ~90min, several dying with "Error: Reached max turns (40)"). mkdir is
# atomic on every POSIX fs and needs no extra binary (macOS ships no `flock` CLI) — a
# second concurrent invocation exits immediately instead of silently corrupting the first.
LOCK_DIR="$CAPAFY_STATE_DIR/.daily_loop.lockdir"
if mkdir "$LOCK_DIR" 2>/dev/null; then
  trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT
else
  # stale-lock guard: a lock dir older than 40min means a prior run crashed without
  # cleaning up (this script's own claude call is timeout-guarded at 1200s=20min) —
  # steal it rather than wedging the loop forever.
  AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0) ))
  if [ "$AGE" -gt 2400 ]; then
    rmdir "$LOCK_DIR" 2>/dev/null; mkdir "$LOCK_DIR" 2>/dev/null
    trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT
  else
    echo "=== $TS daily_loop SKIPPED — another instance holds $LOCK_DIR (age ${AGE}s) ===" >>"$LOG"
    exit 0
  fi
fi

# load env (keys) for the child
set -a; . "$LIFE_MANAGER_STATE_HOME/.env" 2>/dev/null; set +a

echo "=== $TS daily_loop start ===" >> "$LOG"

# Paid buyers can keep using already-listed Agents even when there is no new
# inventory. Check host funding before the healthy-idle exit so an exhausted
# key cannot remain invisible until the next publication attempt.
if ! KEY_HEALTH="$($AUTO/scripts/key_health_gate.sh 5.00 2>&1)"; then
  echo "$TS $KEY_HEALTH" >> "$LOG"
  echo "=== $TS daily_loop done rc=1 (HOST_KEY_UNHEALTHY — paid-user service at risk; marker NOT touched) ===" >> "$LOG"
  exit 1
fi
echo "$TS $KEY_HEALTH" >> "$LOG"

# ── RECONCILE THE LEDGER WITH SERVER TRUTH (self-fix-capafy-loop, 2026-07-07) ──
# state/published.jsonl mirrors the SERVER: every online agent recorded, REVIEW_REJECTED flagged,
# and orphan DRAFT stubs surfaced (2026-07-08) so a half-published card can't rot invisibly.
python3 "$AUTO/scripts/reconcile_ledger.py" --json >> "$LOG" 2>&1 || true

# ── DETERMINISTIC WORK CHECK (self-fix-capafy-loop, 2026-07-08) ──
# Ask the server: is there ANY real work? DRAINED/CAP_FULL = healthy idle → touch the marker and
# SKIP the expensive headless Claude run (protects the subscription quota — no point spending an
# LLM turn to re-discover "nothing to do"). Only PUBLISHABLE fires the publish flow.
INV="$(python3 "$AUTO/scripts/inventory_status.py" 2>>"$LOG")"
VERDICT="$(printf '%s\n' "$INV" | sed -n 's/^VERDICT=//p' | head -1)"
echo "$TS inventory verdict=$VERDICT :: $(printf '%s' "$INV" | tail -1)" >> "$LOG"

# Refresh the durable OFFLINE candidate backlog before any CAP_FULL/DRAINED exit.
# This consumes the inventory response already read above and performs no Capafy write.
printf '%s\n' "$INV" | tail -1 | python3 "$AUTO/scripts/candidate_backlog.py" refresh \
  --inventory-stdin >>"$LOG" 2>&1 || {
    echo "=== $TS candidate backlog refresh failed ===" >>"$LOG"
    exit 1
  }

case "$VERDICT" in
  DRAINED|CAP_FULL)
    touch "$MARK"
    echo "=== $TS daily_loop done rc=0 (HEALTHY-IDLE: $VERDICT — nothing to publish, marker touched, no LLM spend) ===" >> "$LOG"
    exit 0 ;;
  SERVER_UNREADABLE)
    # cannot confirm health → do NOT touch the marker; if this persists the marker goes stale and
    # the healthcheck escalates (auth/network genuinely broken = a real problem to self-fix).
    echo "=== $TS daily_loop done rc=1 (SERVER_UNREADABLE — marker NOT touched; escalates if it persists) ===" >> "$LOG"
    exit 1 ;;
esac

# PRE-run online_count (server truth, from the same $INV that gated us into PUBLISHABLE). This is
# the only reliable "did a listing actually go live" signal — DRAINED/CAP_FULL can also happen when
# a REVIEW_REJECTED item is merely resubmitted into under_review (self-fix-capafy-loop, 2026-07-11:
# this exact confusion produced a false "PUBLISHED" label on a run where nothing went online).
PRE_ONLINE="$(printf '%s' "$INV" | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("online_count", -1))' 2>>"$LOG")"

# PUBLISHABLE → the shared agent runner tries the configured tool-agent providers in order and
# records durable per-attempt evidence. Keep ANTHROPIC_API_KEY unset so a Claude fallback uses the
# authenticated subscription instead of an exhausted pay-as-you-go key.
PROMPT="Follow this runbook exactly and do ONE iteration, terse output: $(cat "$AUTO/DAILY_LOOP.md")
AUTHORITATIVE INVENTORY ACTION: $(printf '%s' "$INV" | tail -1)
Execute exactly that action/item. Do not select or substitute another item from stale ledger or rejection history."
EVIDENCE_DIR="$LIFE_MANAGER_STATE_HOME/state/agent-runner-evidence/capafy-drainer/$(date +%s)-$$"
printf '%s\n' "$PROMPT" | timeout 1200 env -u ANTHROPIC_API_KEY "$RUN_AGENT" \
  --task-class application-lane-agent \
  --evidence-dir "$EVIDENCE_DIR" \
  --task-label capafy-drainer \
  --loop capafy \
  --workdir "$LIFE_MANAGER_REPO" >> "$LOG" 2>&1
RC=$?

# Post-run truth: did a listing actually go live (online_count increased), not just "did the
# rejected/publishable buckets empty out"? A REVIEW_REJECTED item resubmitted into under_review also
# clears those buckets (verdict=DRAINED) without ever going online — that is NOT a publish, it's a
# pending-review state, and must never be logged as PUBLISHED (self-fix-capafy-loop, 2026-07-11).
POST_JSON="$(python3 "$AUTO/scripts/inventory_status.py" 2>>"$LOG")"
POST="$(printf '%s\n' "$POST_JSON" | sed -n 's/^VERDICT=//p' | head -1)"
POST_ONLINE="$(printf '%s' "$POST_JSON" | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin).get("online_count", -1))' 2>>"$LOG")"

# A3 (2026-07-18): a per-pass max-turns exhaustion is a BUDGET limit, not a broken pipeline. Track a
# streak so a single/short exhaustion continues next pass (marker kept fresh) but a PERSISTENT one
# still escalates. Reset on any healthy or differently-failed pass.
MAXTURNS_STREAK="$CAPAFY_STATE_DIR/.maxturns-streak"
if [ "$POST" = "DRAINED" ] || [ "$POST" = "CAP_FULL" ]; then
  touch "$MARK"; echo 0 > "$MAXTURNS_STREAK"
  if [ -n "$PRE_ONLINE" ] && [ -n "$POST_ONLINE" ] && [ "$POST_ONLINE" -gt "$PRE_ONLINE" ] 2>/dev/null; then
    echo "=== $TS daily_loop done rc=$RC (PUBLISHED — online_count $PRE_ONLINE -> $POST_ONLINE, post-verdict=$POST, marker touched) ===" >> "$LOG"
  else
    echo "=== $TS daily_loop done rc=$RC (HEALTHY-IDLE: no new listing went online (online_count unchanged at $POST_ONLINE), post-verdict=$POST, marker touched) ===" >> "$LOG"
  fi
elif tail -60 "$LOG" | grep -q "Reached max turns"; then
  # max-turns exhaustion: the agentic CP1 screenshot loop can legitimately need >60 turns. A single
  # exhausted pass must NOT starve the healthy-pass marker (that fired a self-fix for a non-bug —
  # the 2026-07-17 incident). Bounded continuation: touch the marker + continue for up to 3 passes;
  # only a persistent streak (>=3) is left to escalate. A rejected cfg=1 retry should take the
  # deterministic publish_finish.sh path and never enter the agentic loop at all (DAILY_LOOP.md 2a).
  N=$(( $(cat "$MAXTURNS_STREAK" 2>/dev/null || echo 0) + 1 )); echo "$N" > "$MAXTURNS_STREAK"
  if [ "$N" -lt 3 ]; then
    touch "$MARK"
    echo "=== $TS daily_loop done rc=$RC (MAXTURNS-CONTINUE ${N}/3 — headless agent hit the 60-turn budget mid-publish; marker touched (alive), continues next pass, self-fix NOT escalated) ===" >> "$LOG"
  else
    echo "=== $TS daily_loop done rc=$RC (MAXTURNS-STUCK ${N} passes — persistent budget exhaustion, marker NOT touched → self-fix will escalate) ===" >> "$LOG"
  fi
else
  echo 0 > "$MAXTURNS_STREAK"
  echo "=== $TS daily_loop done rc=$RC (BLOCKED — post-verdict=$POST, marker NOT touched → self-fix will escalate) ===" >> "$LOG"
fi
exit $RC
