#!/usr/bin/env bash
# Daily 22:30 evidence-bound article learning entrypoint.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"
SKILL_DIR="$WRITER_ROOT"
LOG_DIR="$WRITER_LOG_DIR"
mkdir -p "$STATE_DIR" "$LOG_DIR"

# Score the day's title ledger against real high-performing titles before the
# controller reads quality. Nothing in the tree called beat_rate.py, so the
# ledgers the daily run writes were being produced and never scored (measured
# 2026-07-27) -- a loop that looks like it is learning and is not. A failure
# here is a missing data point, never a reason to skip the controller below.
bash "$SKILL_DIR/scripts/score-latest-run.sh" >> "$LOG_DIR/writer-beat-rate.log" 2>&1 || true

# Count what is left to write. The outlet has been instrumented for months and
# the inlet never was; on 2026-07-27 the queue held one card fifteen minutes
# before a publish. Running it here rather than in the morning buys a full day
# of warning, which is the difference between refilling calmly and skipping.
bash "$SKILL_DIR/scripts/topic-supply.sh" >> "$LOG_DIR/writer-topic-supply.log" 2>&1 || true

# Replay-first learning freezes baseline/candidate and completes repeated
# held-out evaluation before it exposes one bounded canary assignment. It does
# not mutate the active playbook and cannot turn 0 -> 0 into a KEEP receipt.
set +e
CLOSE_RESULT="$(python3 "$SKILL_DIR/scripts/writer_learning_worker.py" close-canary \
  --skill-dir "$SKILL_DIR")"
RC=$?
set -e
printf '%s\n' "$CLOSE_RESULT"
if [[ "$RC" -ne 0 ]]; then
  exit "$RC"
fi
CLOSE_STATUS="$(printf '%s' "$CLOSE_RESULT" | jq -er '.status')"
RESULT="$CLOSE_RESULT"
if [[ "$CLOSE_STATUS" == "NO_APPLIED_CANARY" || "$CLOSE_STATUS" == "CYCLE_COMPLETE" ]]; then
  set +e
  RESULT="$(python3 "$SKILL_DIR/scripts/writer_learning_worker.py" offline \
    --skill-dir "$SKILL_DIR")"
  RC=$?
  set -e
  printf '%s\n' "$RESULT"
  if [[ "$RC" -ne 0 ]]; then
    exit "$RC"
  fi
fi

# Preserve the existing exact-publication audit as read-only evidence. It no
# longer owns proposal application or keep/revert.
VERIFY_RESULT="$(python3 "$SKILL_DIR/scripts/self_improve_control.py" verify \
  --skill-dir "$SKILL_DIR")"
printf '%s\n' "$VERIFY_RESULT"

# The canonical report worker reads the new experiment receipt into the same
# Web/Telegram snapshot. Kick it immediately; its durable semantic outbox
# prevents duplicate delivery. If launchd is unavailable, run the same worker
# directly rather than waiting for the next five-minute interval.
# Same executable as ai.anicca.writer-report; direct invocation is immediate
# and also works in a local install before launchd registration.
python3 "$SKILL_DIR/scripts/writer_report_worker.py" --state-dir "$STATE_DIR" || true
