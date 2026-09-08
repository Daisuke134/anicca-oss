#!/usr/bin/env bash
# Durable 300-second publication-contract pending worker. It never creates a run or topic. A crash-truncated
# pre-live target set may initialize only its explicitly missing targets in a publication-free tick.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"
ARTICLE_PROVIDER="claude"
ARTICLE_PROVIDER_COOLDOWN_SECONDS="300"
LOG="${ARTICLE_RESUME_LOG:-$WRITER_LOG_DIR/article-resume.log}"
TELEGRAM_TARGET="${TELEGRAM_TARGET_ID:-${GIG_REPORT_CHAT:-${TELEGRAM_CHAT_ID:-}}}"
MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-$ARTICLE_ROOT/runtime/model-runner.sh}"
MODEL_SUPPORT="${ARTICLE_MODEL_SUPPORT:-$ARTICLE_ROOT/runtime/model-runner-support.py}"
REPAIR_DISPATCH="$ARTICLE_ROOT/scripts/writer_repair_dispatch.py"
QUALITY_REPAIR_CONTROL="${ARTICLE_QUALITY_REPAIR_CONTROL:-$ARTICLE_ROOT/scripts/quality_repair_control.py}"
QUALITY_FEEDBACK_CONTROL="${ARTICLE_QUALITY_FEEDBACK_CONTROL:-$ARTICLE_ROOT/scripts/quality_feedback_recovery.py}"
GENERATION_STATE_CONTROL="$ARTICLE_ROOT/scripts/article_generation_state.py"
PLANNER="$ARTICLE_ROOT/scripts/article_pending.py"
LOCK_DIR="$STATE_DIR/.article-daily.lockdir"
LOCK_OWNER_FILE="$LOCK_DIR/owner.pid"
# Keep nested publisher and gate processes on the same immutable release root.
# Without this export a launchd worker can fall back to an empty compatibility
# directory when the model invokes scripts/run.sh.
ARTICLE_SKILL_DIR="$ARTICLE_ROOT"
# Always propagate the resolved external state root. When ARTICLE_STATE_DIR is
# unset in a launchd environment, exporting the still-empty parameter leaves
# nested managed publishers to fall back to the immutable release's state/.
ARTICLE_STATE_DIR="$STATE_DIR"
export ARTICLE_ROOT ARTICLE_STATE_DIR STATE_DIR ARTICLE_SKILL_DIR \
  ARTICLE_PROVIDER ARTICLE_PROVIDER_COOLDOWN_SECONDS
mkdir -p "$(dirname "$LOG")"

[ -d "$STATE_DIR/runs" ] || exit 0

# A durable local pause is the emergency brake for an external-publication
# worker.  It is checked before lock acquisition, reconciliation, or any
# publisher invocation so a launchd tick cannot create a second side effect
# while a prior failure is being verified.
PUBLICATION_PAUSE_FILE="${ARTICLE_PUBLICATION_PAUSE_FILE:-$STATE_DIR/.publication-paused}"
if [ -f "$PUBLICATION_PAUSE_FILE" ]; then
  # The emergency brake blocks every external effect, but it must not freeze
  # the local repair queue forever.  This narrow mode only releases
  # receipt-backed claims whose Python owner is gone; it cannot select an
  # incident, invoke Codex, execute a runbook, or call a publisher.
  if [ -f "$STATE_DIR/self-heal/incident-queue.json" ]; then
    python3 "$REPAIR_DISPATCH" \
      --state-root "$STATE_DIR" \
      --scripts "$ARTICLE_ROOT/scripts" \
      --registry "$ARTICLE_ROOT/config/repair-runbooks.json" \
      --model-runner "$MODEL_RUNNER" \
      --observed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --recover-claims-only >>"$LOG" 2>&1 || \
      echo "article-resume: paused claim recovery failed closed" >>"$LOG"
  fi
  echo "article-resume: publication paused file=$PUBLICATION_PAUSE_FILE" >>"$LOG"
  exit 0
fi
if [ "${ARTICLE_OWNER_FENCE_ACTIVE:-0}" != "1" ]; then
  OWNER_FENCE_DIR="${ARTICLE_OWNER_FENCE_DIR:-$HOME/.local/state/life-manager/writer/owner-fence}"
  export ARTICLE_OWNER_FENCE_DIR
  exec python3 "$ARTICLE_ROOT/scripts/writer_owner_fence.py" run \
    --fence-dir "$OWNER_FENCE_DIR" --owner article-resume \
    --root "$ARTICLE_ROOT" --state "$STATE_DIR" \
    --run-id "${ARTICLE_EXPECTED_RUN_ID:-daily-$(TZ=Asia/Tokyo date +%F)}" \
    -- "$0" "$@"
fi

# A publisher must not create an irreversible external effect when its durable
# receipt, circuit, or outbox cannot be persisted. Resume has no cleanup rights;
# Life Manager's shared disk admission defaults to 524288 KiB. Keep direct
# owner wakes identical to the supervised guard instead of inventing a second
# Writer-only threshold.
CANONICAL_DISK_HEADROOM_KIB=524288
GIG_DISK_HEADROOM_KIB="${GIG_DISK_HEADROOM_KIB:-$CANONICAL_DISK_HEADROOM_KIB}"
export GIG_DISK_HEADROOM_KIB
case "$GIG_DISK_HEADROOM_KIB" in
  ''|*[!0-9]*|0)
    echo "article-resume: disk floor configuration invalid" >>"$LOG"
    exit 1
    ;;
esac
if [ -n "${ARTICLE_RESUME_MIN_FREE_BYTES:-}" ]; then
  ARTICLE_DISK_MIN_FREE_BYTES="$ARTICLE_RESUME_MIN_FREE_BYTES"
  export ARTICLE_DISK_MIN_FREE_BYTES
fi
DISK_MIN_FREE_BYTES="$(python3 "$ARTICLE_ROOT/scripts/writer_capacity_floor.py" --state-dir "$STATE_DIR")" || {
  echo "article-resume: capacity receipt invalid" >>"$LOG"
  exit 1
}
case "$DISK_MIN_FREE_BYTES" in
  ''|*[!0-9]*|0)
    echo "article-resume: disk floor configuration invalid" >>"$LOG"
    exit 1
    ;;
esac
if [ "$GIG_DISK_HEADROOM_KIB" -lt "$CANONICAL_DISK_HEADROOM_KIB" ] \
  || [ "$DISK_MIN_FREE_BYTES" -lt "$((CANONICAL_DISK_HEADROOM_KIB * 1024))" ]; then
  echo "article-resume: disk floor configuration below canonical minimum" >>"$LOG"
  exit 1
fi
disk_free_bytes() {
  local free_kb
  free_kb="$(df -Pk / 2>/dev/null | awk 'NR==2{print $4}')"
  echo $(( ${free_kb:-0} * 1024 ))
}
DISK_FREE_BYTES="$(disk_free_bytes)"
if [ "${DISK_FREE_BYTES:-0}" -lt "$DISK_MIN_FREE_BYTES" ]; then
  echo "article-resume: disk floor blocked publication free=${DISK_FREE_BYTES}bytes required=${DISK_MIN_FREE_BYTES}bytes" >>"$LOG"
  exit 1
fi

process_owns_this_publication_lock() {
  local owner_pid="$1" owner_command owner_cwd token token_dir token_base resolved
  owner_command="$(ps -p "$owner_pid" -o command= 2>/dev/null || true)"
  [ -n "$owner_command" ] || return 1
  for token in $owner_command; do
    case "$token" in
      "$ARTICLE_ROOT/article-daily.sh"|\
      "$ARTICLE_ROOT/scripts/article-resume-pending.sh"|\
      "$ARTICLE_ROOT/scripts/zenn-deferred-worker.py"|\
      "$ARTICLE_ROOT/scripts/current_run_media_upgrade.py") return 0 ;;
    esac
  done
  owner_cwd="$(/usr/sbin/lsof -a -p "$owner_pid" -d cwd -Fn 2>/dev/null \
    | sed -n 's/^n//p' | head -1)"
  [ -n "$owner_cwd" ] || return 1
  for token in $owner_command; do
    case "$token" in
      *article-daily.sh|*article-resume-pending.sh|\
      *zenn-deferred-worker.py|*current_run_media_upgrade.py)
        case "$token" in /*) resolved="$token" ;; *)
          token_dir="$(dirname "$token")"
          token_base="$(basename "$token")"
          resolved="$(cd "$owner_cwd/$token_dir" 2>/dev/null \
            && printf '%s/%s' "$PWD" "$token_base")"
          ;;
        esac
        case "$resolved" in
          "$ARTICLE_ROOT/article-daily.sh"|\
          "$ARTICLE_ROOT/scripts/article-resume-pending.sh"|\
          "$ARTICLE_ROOT/scripts/zenn-deferred-worker.py"|\
          "$ARTICLE_ROOT/scripts/current_run_media_upgrade.py") return 0 ;;
        esac
        ;;
    esac
  done
  return 1
}

release_publication_lock() {
  local recorded_owner=""
  recorded_owner="$(cat "$LOCK_OWNER_FILE" 2>/dev/null || true)"
  if [ "$recorded_owner" = "$$" ]; then
    rm -f -- "$LOCK_OWNER_FILE"
    rmdir "$LOCK_DIR" 2>/dev/null
  else
    return 1
  fi
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  # A SIGTERM between mkdir and the EXIT trap can leave an empty ownerless
  # directory forever. Recover only that exact shape, and only after proving
  # no *other* publication worker process is alive. Unexpected lock contents
  # remain fail-closed.
  OTHER_OWNER=""
  RECORDED_OWNER="$(cat "$LOCK_OWNER_FILE" 2>/dev/null || true)"
  case "$RECORDED_OWNER" in
    '') ;;
    *[!0-9]*) OTHER_OWNER="invalid-owner-record" ;;
    *)
      if [ "$RECORDED_OWNER" != "$$" ] \
        && kill -0 "$RECORDED_OWNER" 2>/dev/null \
        && process_owns_this_publication_lock "$RECORDED_OWNER"; then
        OTHER_OWNER="$RECORDED_OWNER"
      fi
      ;;
  esac
  # Match process basenames, not only absolute command strings. A manual
  # kickstart such as `bash skills/writer-agent/scripts/article-resume-pending.sh`
  # is the same owner as launchd's absolute invocation; the old absolute
  # pgrep missed it and let launchd steal the empty directory lock.
  for OWNER_COMMAND in \
    'article-daily\.sh' \
    'article-resume-pending\.sh' \
    'zenn-deferred-worker\.py' \
    'current_run_media_upgrade\.py'; do
    while IFS= read -r OWNER_PID; do
      if [ -n "$OWNER_PID" ] && [ "$OWNER_PID" != "$$" ] \
        && process_owns_this_publication_lock "$OWNER_PID"; then
        OTHER_OWNER="$OWNER_PID"
        break 2
      fi
    done < <(pgrep -f "$OWNER_COMMAND" 2>/dev/null || true)
  done
  # article-daily records both PID and process-start time. Treat those two
  # files as lock metadata; older recovery code whitelisted owner.pid only and
  # left every stale lock permanently wedged after a normal exit.
  LOCK_EXTRA="$(find "$LOCK_DIR" -mindepth 1 -maxdepth 1 \
    ! -name owner.pid ! -name owner.start -print -quit 2>/dev/null)"
  if [ -z "$OTHER_OWNER" ] \
    && { [ -z "$RECORDED_OWNER" ] \
      || { [ -f "$LOCK_OWNER_FILE" ] && [ -f "$LOCK_DIR/owner.start" ] \
        && [ -z "$LOCK_EXTRA" ] \
        && rm -f -- "$LOCK_OWNER_FILE" "$LOCK_DIR/owner.start"; }; } \
    && [ -z "$(find "$LOCK_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ] \
    && rmdir "$LOCK_DIR" 2>/dev/null \
    && mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "article-resume: recovered ownerless publication lock" >>"$LOG"
  else
    echo "article-resume: shared publication lock is held" >>"$LOG"
    exit 0
  fi
fi
printf '%s\n' "$$" >"$LOCK_OWNER_FILE" || {
  rmdir "$LOCK_DIR" 2>/dev/null || true
  exit 1
}
trap 'release_publication_lock' EXIT

# The same 300-second reconciler owns both sides of the publication boundary.
# A safe interrupted generation has no public side effect, so hand the exact
# saved run/prompt back to the daily wrapper; that wrapper revalidates the
# immutable boundary and invokes it once. Never create a run from this worker.
START_CONTROL="${ARTICLE_START_CONTROL:-$ARTICLE_ROOT/scripts/article_daily_start_control.py}"
LOCAL_DATE="${ARTICLE_LOCAL_DATE:-$(TZ=Asia/Tokyo date +%F)}"
QUALITY_LEDGER="$STATE_DIR/articles.jsonl"
PRE_START_DECISION="$(python3 "$START_CONTROL" \
  --state-dir "$STATE_DIR" --local-date "$LOCAL_DATE" 2>>"$LOG" || \
  printf '%s' '{"action":"block-incomplete","reason":"start-control-error"}')"
PRE_START_ACTION="$(printf '%s' "$PRE_START_DECISION" | jq -r '.action // empty')"
PRE_START_REASON="$(printf '%s' "$PRE_START_DECISION" | jq -r '.reason // empty')"
ADOPTION_ACTIVE=0
# A calendar rollover must not strand the exact unpublished run that already
# has durable pending ledger rows. Prefer the start controller's selected run;
# otherwise adopt only one ledger-backed candidate. Multiple candidates are
# ambiguous and fail closed instead of silently choosing or creating a run.
ADOPTION_RUN_ID="$(python3 - "$STATE_DIR" "$QUALITY_LEDGER" "$PRE_START_DECISION" <<'PY'
import json
from pathlib import Path
import re
import sys

state_root = Path(sys.argv[1]).resolve()
ledger = Path(sys.argv[2]).resolve()
decision = json.loads(sys.argv[3])
runs = state_root / "runs"
allowed = {"provider-failed-ambiguous", "quality-repair-ready"}

def status(run_id):
    if not re.fullmatch(r"(?:daily-\d{4}-\d{2}-\d{2}|\d{8}-\d{6})", run_id):
        return None
    run_dir = runs / run_id
    state_path = run_dir / "gates/generation-state.json"
    prompt = run_dir / "article-daily-prompt.txt"
    if state_path.is_symlink() or prompt.is_symlink():
        raise ValueError("adoption evidence is not regular")
    if not state_path.is_file() or not prompt.is_file():
        return None
    value = json.loads(state_path.read_text(encoding="utf-8"))
    return value.get("status")

selected = decision.get("run_id")
if isinstance(selected, str) and status(selected) in allowed:
    print(selected)
    raise SystemExit(0)

ledger_ids = set()
if ledger.is_file() and not ledger.is_symlink():
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        run_id = row.get("run_id") if isinstance(row, dict) else None
        if isinstance(run_id, str) and run_id:
            ledger_ids.add(run_id)
candidates = sorted(run_id for run_id in ledger_ids if status(run_id) in allowed)
if len(candidates) > 1:
    raise ValueError("multiple ledger-backed adoption candidates")
if candidates:
    print(candidates[0])
PY
)" || {
  echo "article-resume: prepublication adoption selection failed closed" >>"$LOG"
  exit 1
}
PREVALIDATED_QUALITY_PLAN=""
if [ -n "$ADOPTION_RUN_ID" ]; then
  ADOPTION_RUN_DIR="$STATE_DIR/runs/$ADOPTION_RUN_ID"
  QUALITY_REPAIR_STATE_PATH="$ADOPTION_RUN_DIR/gates/quality-repair-state.json"
  GENERATION_STATUS="$(jq -r '.status // empty' \
    "$ADOPTION_RUN_DIR/gates/generation-state.json" 2>/dev/null || true)"
  QUALITY_REPAIR_STATE_PRESENT=0
  if [ -e "$QUALITY_REPAIR_STATE_PATH" ] || [ -L "$QUALITY_REPAIR_STATE_PATH" ]; then
    QUALITY_REPAIR_STATE_PRESENT=1
  fi
  if [ "$QUALITY_REPAIR_STATE_PRESENT" -eq 1 ] \
    && [ "$GENERATION_STATUS" != "quality-repair-ready" ]; then
    echo "article-resume: quality repair state conflicts with generation status run=$ADOPTION_RUN_ID" >>"$LOG"
    exit 1
  fi
  if [ "$GENERATION_STATUS" = "quality-repair-ready" ] \
    && [ "$QUALITY_REPAIR_STATE_PRESENT" -eq 1 ]; then
    if [ -L "$QUALITY_REPAIR_STATE_PATH" ] \
      || [ ! -f "$QUALITY_REPAIR_STATE_PATH" ] \
      || ! jq -e 'type == "object" and length > 0' \
        "$QUALITY_REPAIR_STATE_PATH" >/dev/null 2>>"$LOG"; then
      echo "article-resume: quality repair state invalid; adoption blocked run=$ADOPTION_RUN_ID" >>"$LOG"
      exit 1
    fi
    ADOPTION_ACTION="quality-repair-state-present"
    ADOPTION_STATUS="quality-repair-ready"
    PREVALIDATED_QUALITY_PLAN_RC=0
    PREVALIDATED_QUALITY_PLAN="$(python3 "$QUALITY_REPAIR_CONTROL" plan \
      --run-dir "$ADOPTION_RUN_DIR" --ledger "$QUALITY_LEDGER" 2>>"$LOG")" \
      || PREVALIDATED_QUALITY_PLAN_RC=$?
    if ! PREVALIDATED_STATUS="$(printf '%s' "$PREVALIDATED_QUALITY_PLAN" | jq -r '.status // empty' 2>/dev/null)" \
      || ! PREVALIDATED_REASON="$(printf '%s' "$PREVALIDATED_QUALITY_PLAN" | jq -r '.reason // empty' 2>/dev/null)"; then
      echo "article-resume: quality repair prevalidation result malformed run=$ADOPTION_RUN_ID" >>"$LOG"
      exit 1
    fi
    if [ "$PREVALIDATED_QUALITY_PLAN_RC" -eq 0 ] \
      && [ "$PREVALIDATED_STATUS" = "READY" ]; then
      if ! jq -e \
        --arg run_id "$ADOPTION_RUN_ID" --arg run_dir "$ADOPTION_RUN_DIR" \
        --slurpfile repair_state "$QUALITY_REPAIR_STATE_PATH" \
        '(.status == "READY")
         and (.run_id == $run_id)
         and (.run_dir == $run_dir)
         and ($repair_state | length == 1)
         and ($repair_state[0] | type == "object")
         and ((.repair_epoch | type) == "number")
         and ((.repair_epoch | floor) == .repair_epoch)
         and (.repair_epoch >= 0)
         and ((.attempts | type) == "number")
         and ((.attempts | floor) == .attempts)
         and (.attempts >= 0)
         and (($repair_state[0].repair_epoch | type) == "number")
         and (($repair_state[0].repair_epoch | floor) == $repair_state[0].repair_epoch)
         and (($repair_state[0].repair_epoch) >= 0)
         and (($repair_state[0].attempts | type) == "number")
         and (($repair_state[0].attempts | floor) == $repair_state[0].attempts)
         and (($repair_state[0].attempts) >= 0)
         and (.repair_epoch == $repair_state[0].repair_epoch)
         and (.attempts == $repair_state[0].attempts)
         and (
           (.reason == "structurally-exhausted-quality-evaluations"
            and ((keys | sort) == ["attempts","reason","repair_epoch","run_dir","run_id","status"])
            and (($repair_state[0].status | type) == "string"))
           or
           (.reason == "prepared-quality-repair"
            and ((keys | sort) == ["attempts","prompt_path","prompt_sha256","reason","repair_epoch","run_dir","run_id","status"])
            and (($repair_state[0].prompt_path | type) == "string")
            and (($repair_state[0].prompt_sha256 | type) == "string")
            and (($repair_state[0].prompt_sha256 | test("^[0-9a-f]{64}$")))
            and ((.prompt_path | type) == "string")
            and ((.prompt_sha256 | type) == "string")
            and ((.prompt_sha256 | test("^[0-9a-f]{64}$")))
            and (.prompt_path == $repair_state[0].prompt_path)
            and (.prompt_sha256 == $repair_state[0].prompt_sha256))
           or
           ((.reason == "tracked-active-editorial-repair-source-defect" or .reason == "tracked-repair-owner-prompt-source-defect")
            and ((keys | sort) == ["attempts","prompt_path","prompt_sha256","reason","repair_epoch","run_dir","run_id","status"])
            and $repair_state[0].status == "terminal-incomplete"
            and $repair_state[0].attempts == 2 and $repair_state[0].quality_action == "evaluate_reroute" and $repair_state[0].source_defect == "reader-terminal-receipt"
            and (.prompt_path == $repair_state[0].prompt_path) and (.prompt_sha256 == $repair_state[0].prompt_sha256))
           or
           (.reason == "orphaned-owner-prompt-recovery"
            and ((keys | sort) == ["attempts","orphaned_owner_pid","prompt_path","prompt_sha256","reason","repair_epoch","run_dir","run_id","status"])
            and $repair_state[0].status == "invoking" and $repair_state[0].attempts == 2
            and $repair_state[0].quality_action == "evaluate_reroute" and $repair_state[0].source_defect == "reader-terminal-receipt"
            and (($repair_state[0].source_recovery_receipt_sha256 | type) == "string") and (($repair_state[0].owner_recovery_receipt_sha256 | type) == "string")
            and (.orphaned_owner_pid == $repair_state[0].owner_pid)
            and (.prompt_path == $repair_state[0].prompt_path) and (.prompt_sha256 == $repair_state[0].prompt_sha256))
           or
           (.reason == "orphaned-quality-repair"
            and ((keys | sort) == ["attempts","orphaned_owner_pid","prompt_path","prompt_sha256","reason","repair_epoch","run_dir","run_id","status"])
            and (($repair_state[0].prompt_path | type) == "string")
            and (($repair_state[0].prompt_sha256 | type) == "string")
            and (($repair_state[0].prompt_sha256 | test("^[0-9a-f]{64}$")))
            and ((.prompt_path | type) == "string")
            and ((.prompt_sha256 | type) == "string")
            and ((.prompt_sha256 | test("^[0-9a-f]{64}$")))
            and (.prompt_path == $repair_state[0].prompt_path)
            and (.prompt_sha256 == $repair_state[0].prompt_sha256)
            and ((.orphaned_owner_pid | type) == "number")
            and ((.orphaned_owner_pid | floor) == .orphaned_owner_pid)
            and (.orphaned_owner_pid > 0))
         )' \
        <<<"$PREVALIDATED_QUALITY_PLAN" >/dev/null; then
        echo "article-resume: quality repair prevalidation identity invalid run=$ADOPTION_RUN_ID" >>"$LOG"
        exit 1
      fi
    elif [ "$PREVALIDATED_QUALITY_PLAN_RC" -eq 1 ] \
      && [ "$PREVALIDATED_STATUS" = "REFUSED" ] \
      && [ "$PREVALIDATED_REASON" = "quality-repair-already-terminal-blocked" ] \
      && jq -e '((keys | sort) == ["reason","status"])' \
        <<<"$PREVALIDATED_QUALITY_PLAN" >/dev/null 2>&1; then
      :
    else
      echo "article-resume: quality repair prevalidation refused run=$ADOPTION_RUN_ID reason=$PREVALIDATED_REASON rc=$PREVALIDATED_QUALITY_PLAN_RC" >>"$LOG"
      exit 1
    fi
  else
    ADOPTION_RESULT="$(python3 "$GENERATION_STATE_CONTROL" \
      --run-dir "$ADOPTION_RUN_DIR" --run-id "$ADOPTION_RUN_ID" \
      --prompt-file "$ADOPTION_RUN_DIR/article-daily-prompt.txt" \
      --ledger "$QUALITY_LEDGER" adopt-prepublication 2>/dev/null)" || {
      echo "article-resume: prepublication adoption failed closed run=$ADOPTION_RUN_ID" >>"$LOG"
      exit 1
    }
    ADOPTION_ACTION="$(printf '%s' "$ADOPTION_RESULT" | jq -r '.action // empty')"
    ADOPTION_STATUS="$(printf '%s' "$ADOPTION_RESULT" | jq -r '.status // empty')"
  fi
  case "$ADOPTION_ACTION/$ADOPTION_STATUS" in
    adopted/quality-repair-ready|recovered/quality-repair-ready|unchanged/quality-repair-ready|quality-repair-state-present/quality-repair-ready) ;;
    *)
      echo "article-resume: prepublication adoption result invalid run=$ADOPTION_RUN_ID" >>"$LOG"
      exit 1
      ;;
  esac
  ADOPTION_ACTIVE=1
  PRE_START_DECISION="$(jq -cn --arg run_id "$ADOPTION_RUN_ID" \
    '{action:"resume-prepublication-adoption",run_id:$run_id,reason:"quality-repair-ready"}')"
  PRE_START_ACTION="resume-prepublication-adoption"
  PRE_START_REASON="quality-repair-ready"
  echo "article-resume: prepublication adoption run=$ADOPTION_RUN_ID action=$ADOPTION_ACTION status=$ADOPTION_STATUS" >>"$LOG"
fi
# A quality recovery is an unpublished obligation, not a same-day article. At
# midnight, do not let the calendar selector hide a prior-day run whose Codex
# feedback budget is still live. The controller is read-only here; the normal
# owner fence and quality recovery state machine still own every side effect.
PENDING_QUALITY_RUN_ID=""
if [ "$PRE_START_ACTION" != "skip-pending-worker" ] \
  && [ "$ADOPTION_ACTIVE" -ne 1 ]; then
  PENDING_QUALITY_RUN_ID="$(python3 - "$STATE_DIR" "$QUALITY_LEDGER" "$QUALITY_FEEDBACK_CONTROL" <<'PY'
import importlib.util
from pathlib import Path
import sys

state_root = Path(sys.argv[1]).resolve()
ledger = Path(sys.argv[2]).resolve()
control_path = Path(sys.argv[3]).resolve()
spec = importlib.util.spec_from_file_location("quality_feedback_recovery_scan", control_path)
control = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(control)
if not state_root.is_dir() or not (state_root / "runs").is_dir():
    raise SystemExit(0)
candidates = sorted(
    (path for path in (state_root / "runs").iterdir() if path.is_dir()),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
accepted = {
    "terminal-quality-feedback",
    "prepared-quality-feedback-recovery",
    "orphaned-quality-feedback-recovery",
    "terminal-quality-publication-handoff",
    "prepared-quality-publication-handoff",
    "orphaned-quality-publication-handoff",
}
for candidate in candidates:
    try:
        result = control.plan(candidate, ledger)
    except Exception:
        continue
    if result.get("status") == "READY" and result.get("reason") in accepted:
        print(candidate.name)
        break
PY
)"
fi
if [ -n "$PENDING_QUALITY_RUN_ID" ]; then
  PRE_START_DECISION="$(jq -cn \
    --arg run_id "$PENDING_QUALITY_RUN_ID" \
    '{action:"resume-quality-feedback",run_id:$run_id,reason:"pending-quality-recovery-across-calendar-day"}')"
  PRE_START_ACTION="resume-quality-feedback"
  PRE_START_REASON="pending-quality-recovery-across-calendar-day"
  echo "article-resume: prioritizing unpublished quality recovery run=$PENDING_QUALITY_RUN_ID local=$LOCAL_DATE" >>"$LOG"
fi
# A persisted publication backlog is the foreground availability contract.
# Inspect it before today's schedule decision so midnight and quality work
# cannot starve an older immutable active-four run.
PRIORITY_PUBLICATION_PLAN='{"status":"SKIPPED","reason":"start-control-blocked"}'
PRIORITY_PUBLICATION_READY=0
if [ "$PRE_START_ACTION" != "block-incomplete" ]; then
  PRIORITY_PLAN_ARGS=(--state-root "$STATE_DIR")
  [ -n "${ARTICLE_NOW:-}" ] && PRIORITY_PLAN_ARGS+=(--now "$ARTICLE_NOW")
  PRIORITY_PUBLICATION_PLAN="$(python3 "$PLANNER" "${PRIORITY_PLAN_ARGS[@]}" 2>>"$LOG" || true)"
  if [ "$(printf '%s' "$PRIORITY_PUBLICATION_PLAN" | jq -r '.status // empty')" = "READY" ] \
    && [ "$(printf '%s' "$PRIORITY_PUBLICATION_PLAN" | jq '((.initialization_pairs // []) + (.eligible_pairs // []) + (.recovery_pairs // [])) | length')" -gt 0 ]; then
    PRIORITY_RUN_ID="$(printf '%s' "$PRIORITY_PUBLICATION_PLAN" | jq -r '.run_id // empty')"
    PRIORITY_RUN_DATE=""
    if [ -n "$PRIORITY_RUN_ID" ]; then
      PRIORITY_RUN_DATE="$(python3 - "$PRIORITY_RUN_ID" <<'PY' 2>/dev/null
from datetime import date, datetime
import re
import sys

run_id = sys.argv[1]
if re.fullmatch(r"daily-\d{4}-\d{2}-\d{2}", run_id):
    parsed = date.fromisoformat(run_id.removeprefix("daily-"))
elif re.fullmatch(r"\d{8}-\d{6}", run_id):
    parsed = datetime.strptime(run_id, "%Y%m%d-%H%M%S").date()
else:
    raise SystemExit(1)
print(parsed.isoformat())
PY
)" || PRIORITY_RUN_DATE=""
    fi
    # An older backlog must not suppress today's article, but unknown, invalid,
    # or future run identities fail closed so they cannot create a duplicate
    # daily run. Only a calendar date proven strictly before LOCAL_DATE opts
    # out of the foreground publication signal.
    PRIORITY_OLDER_RUN=0
    if [ -n "$PRIORITY_RUN_DATE" ] && python3 - "$PRIORITY_RUN_DATE" "$LOCAL_DATE" <<'PY'
from datetime import date
import sys

try:
    run_date = date.fromisoformat(sys.argv[1])
    local_date = date.fromisoformat(sys.argv[2])
except (IndexError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if run_date < local_date else 1)
PY
    then
      PRIORITY_OLDER_RUN=1
    fi
    if [ "$PRIORITY_OLDER_RUN" -eq 1 ]; then
      echo "article-resume: older publication backlog does not block daily schedule run=$PRIORITY_RUN_ID date=$PRIORITY_RUN_DATE local=$LOCAL_DATE" >>"$LOG"
    else
      PRIORITY_PUBLICATION_READY=1
    fi
  fi
fi
# The 06:00 calendar job remains the normal creator. If the host was asleep,
# launchd was unloaded, or that event was otherwise missed, the five-minute
# reconciler may catch up only the explicit no-run schedule miss. A completed
# run is a normal release for a future calendar tick, not a missed-schedule
# signal; otherwise every 300-second tick would create another article.
LOCAL_HOUR="${ARTICLE_LOCAL_HOUR:-$(TZ=Asia/Tokyo date +%H)}"
DAILY_SCHEDULE_HOUR="${ARTICLE_DAILY_SCHEDULE_HOUR:-6}"
case "$LOCAL_HOUR" in
  0[0-9]|1[0-9]|2[0-3]) ;;
  *) echo "article-resume: invalid local hour; catch-up blocked" >>"$LOG"; exit 1 ;;
esac
case "$DAILY_SCHEDULE_HOUR" in
  [0-9]|1[0-9]|2[0-3]) ;;
  *) echo "article-resume: invalid daily schedule hour; catch-up blocked" >>"$LOG"; exit 1 ;;
esac
LOCAL_HOUR_VALUE="${LOCAL_HOUR#0}"
[ -n "$LOCAL_HOUR_VALUE" ] || LOCAL_HOUR_VALUE=0
if [ "$PRE_START_ACTION" = "new" ] \
  && [ "$PRE_START_REASON" = "no-same-jst-day-run" ] \
  && [ "$PRIORITY_PUBLICATION_READY" -ne 1 ]; then
  if [ "$LOCAL_HOUR_VALUE" -lt "$DAILY_SCHEDULE_HOUR" ]; then
    echo "article-resume: daily schedule not due date=$LOCAL_DATE hour=$LOCAL_HOUR" >>"$LOG"
    exit 0
  fi
  echo "article-resume: missed daily schedule catch-up date=$LOCAL_DATE hour=$LOCAL_HOUR" >>"$LOG"
  export ARTICLE_EXPECTED_NEW_DAILY_DATE="$LOCAL_DATE"
  trap - EXIT
  release_publication_lock || exit 1
  exec bash "$ARTICLE_ROOT/article-daily.sh"
fi

GENERATION_RUN_ID="$(printf '%s' "$PRE_START_DECISION" | jq -r '.run_id // empty')"
case "$GENERATION_RUN_ID" in
  daily-????-??-??|????????-??????) ;;
  *) GENERATION_RUN_ID="daily-$LOCAL_DATE" ;;
esac
GENERATION_RUN_DIR="$STATE_DIR/runs/$GENERATION_RUN_ID"
GENERATION_STATE_PATH="$GENERATION_RUN_DIR/gates/generation-state.json"

# A replacement that consumed its one normal reroute may receive one research-first
# recovery on the same run. It runs before legacy/source-defect repair because its
# terminal receipts are current and its missing evidence is the actual input.
QUALITY_FEEDBACK_PLAN="$(python3 "$QUALITY_FEEDBACK_CONTROL" plan \
  --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" 2>>"$LOG" || true)"
if [ "$PRIORITY_PUBLICATION_READY" -ne 1 ] \
  && [ "$(printf '%s' "$QUALITY_FEEDBACK_PLAN" | jq -r '.status // empty')" = "READY" ]; then
  QUALITY_FEEDBACK_REASON="$(printf '%s' "$QUALITY_FEEDBACK_PLAN" | jq -r '.reason // empty')"
  if [ "$QUALITY_FEEDBACK_REASON" = "terminal-quality-feedback" ]; then
    QUALITY_FEEDBACK_READY="$(python3 "$QUALITY_FEEDBACK_CONTROL" begin \
      --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" 2>>"$LOG")" || {
      echo "article-resume: bounded quality feedback begin failed closed" >>"$LOG"
      exit 1
    }
  elif [ "$QUALITY_FEEDBACK_REASON" = "terminal-quality-publication-handoff" ]; then
    QUALITY_FEEDBACK_READY="$(python3 "$QUALITY_FEEDBACK_CONTROL" handoff \
      --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" 2>>"$LOG")" || {
      echo "article-resume: quality publication handoff failed closed" >>"$LOG"
      exit 1
    }
  else
    QUALITY_FEEDBACK_READY="$QUALITY_FEEDBACK_PLAN"
  fi
  QUALITY_FEEDBACK_RUN_ID="$(printf '%s' "$QUALITY_FEEDBACK_READY" | jq -r '.run_id // empty')"
  [ -n "$QUALITY_FEEDBACK_RUN_ID" ] || QUALITY_FEEDBACK_RUN_ID="$(basename "$GENERATION_RUN_DIR")"
  QUALITY_FEEDBACK_PROMPT="$(printf '%s' "$QUALITY_FEEDBACK_READY" | jq -r '.prompt_path // empty')"
  if [ ! -f "$QUALITY_FEEDBACK_PROMPT" ]; then
    echo "article-resume: bounded quality feedback prompt missing" >>"$LOG"
    exit 1
  fi
  PROVIDER="${ARTICLE_PROVIDER:-codex}"
  HEALTH_FILE="${ARTICLE_PROVIDER_HEALTH:-$STATE_DIR/provider-health.json}"
  PROVIDER_READY=0
  for CANDIDATE in $([ "$PROVIDER" = "auto" ] && printf 'codex claude' || printf '%s' "$PROVIDER"); do
    if python3 "$MODEL_SUPPORT" eligible --file "$HEALTH_FILE" \
      --provider "$CANDIDATE" --mode agent >/dev/null 2>&1; then
      PROVIDER_READY=1
      break
    fi
  done
  if [ "$PROVIDER_READY" -ne 1 ]; then
    echo "article-resume: bounded quality feedback remains in provider cooldown run=$QUALITY_FEEDBACK_RUN_ID" >>"$LOG"
    exit 0
  fi
  QUALITY_FEEDBACK_INVOKING="$(python3 "$QUALITY_FEEDBACK_CONTROL" invoke \
    --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" \
    --owner-pid "$$" 2>>"$LOG")" || {
    echo "article-resume: bounded quality feedback invoke failed closed run=$QUALITY_FEEDBACK_RUN_ID" >>"$LOG"
    exit 1
  }
  printf '%s\n' "$QUALITY_FEEDBACK_INVOKING" >>"$LOG"
  INVOKING_FEEDBACK_PROMPT="$(printf '%s' "$QUALITY_FEEDBACK_INVOKING" | jq -r '.prompt_path // empty')"
  [ -n "$INVOKING_FEEDBACK_PROMPT" ] && QUALITY_FEEDBACK_PROMPT="$INVOKING_FEEDBACK_PROMPT"
  export ARTICLE_RUN_DIR="$GENERATION_RUN_DIR"
  export ARTICLE_QUALITY_FEEDBACK_ACTIVE=1
  export ARTICLE_QUALITY_FEEDBACK_OWNER_PID="$$"
  ARTICLE_MODEL_LOG="$LOG" bash "$ARTICLE_ROOT/runtime/judge-broker.sh" "$GENERATION_RUN_DIR" &
  JUDGE_BROKER_PID=$!
  ARTICLE_RUN_ID="$QUALITY_FEEDBACK_RUN_ID" ARTICLE_MODEL_LOG="$LOG" \
    "$MODEL_RUNNER" agent --prompt-file "$QUALITY_FEEDBACK_PROMPT"
  RC=$?
  kill "$JUDGE_BROKER_PID" 2>/dev/null || true
  wait "$JUDGE_BROKER_PID" 2>/dev/null || true
  python3 "$QUALITY_FEEDBACK_CONTROL" result \
    --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" \
    --return-code "$RC" --owner-pid "$$" >>"$LOG" 2>&1 || {
    echo "article-resume: bounded quality feedback result failed closed run=$QUALITY_FEEDBACK_RUN_ID rc=$RC" >>"$LOG"
    exit 1
  }
  echo "article-resume: bounded quality feedback recovery run=$QUALITY_FEEDBACK_RUN_ID rc=$RC" >>"$LOG"
  exit 0
fi

# Reopen only an exact unpublished legacy block or a hash-bound tracked source
# defect whose fixed gate now passes. The start controller selects the newest
# same-day run, including timestamp replacement IDs. This runs before the
# generic publication planner so older backlog cannot starve today's article.
if [ -n "$PREVALIDATED_QUALITY_PLAN" ]; then
  QUALITY_PLAN="$PREVALIDATED_QUALITY_PLAN"
else
  QUALITY_PLAN="$(python3 "$QUALITY_REPAIR_CONTROL" plan \
    --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" 2>>"$LOG" || true)"
fi
if [ "$PRIORITY_PUBLICATION_READY" -ne 1 ] \
  && [ "$(printf '%s' "$QUALITY_PLAN" | jq -r '.status // empty')" = "READY" ]; then
  QUALITY_REASON="$(printf '%s' "$QUALITY_PLAN" | jq -r '.reason // empty')"
  if [ "$QUALITY_REASON" = "structurally-exhausted-quality-evaluations" ]; then
    python3 "$QUALITY_REPAIR_CONTROL" terminalize \
      --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" >>"$LOG" 2>&1 || {
      echo "article-resume: deterministic quality terminalization failed closed" >>"$LOG"
      exit 1
    }
    echo "article-resume: deterministic quality terminalization run=$GENERATION_RUN_ID" >>"$LOG"
    exit 0
  elif [ "$QUALITY_REASON" = "legacy-stale-quality-block" ] \
    || [ "$QUALITY_REASON" = "tracked-bookmark-source-defect" ] \
    || [ "$QUALITY_REASON" = "tracked-reader-terminal-source-defect" ] \
    || [ "$QUALITY_REASON" = "tracked-reader-terminal-receipt-source-defect" ] \
    || [ "$QUALITY_REASON" = "tracked-editorial-hash-scope-source-defect" ] \
    || [ "$QUALITY_REASON" = "tracked-topic-router-reroute-source-defect" ]; then
    QUALITY_READY="$(python3 "$QUALITY_REPAIR_CONTROL" begin \
      --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" 2>>"$LOG")" || {
      echo "article-resume: bounded quality repair begin failed closed" >>"$LOG"
      exit 1
    }
  else
    QUALITY_READY="$QUALITY_PLAN"
  fi
  QUALITY_RUN_ID="$(printf '%s' "$QUALITY_READY" | jq -r '.run_id // empty')"
  [ -n "$QUALITY_RUN_ID" ] || QUALITY_RUN_ID="$(basename "$GENERATION_RUN_DIR")"
  QUALITY_PROMPT="$(printf '%s' "$QUALITY_READY" | jq -r '.prompt_path // empty')"
  if [ ! -f "$QUALITY_PROMPT" ]; then
    echo "article-resume: bounded quality repair prompt missing" >>"$LOG"
    exit 1
  fi
  PROVIDER="${ARTICLE_PROVIDER:-codex}"
  HEALTH_FILE="${ARTICLE_PROVIDER_HEALTH:-$STATE_DIR/provider-health.json}"
  PROVIDER_READY=0
  for CANDIDATE in $([ "$PROVIDER" = "auto" ] && printf 'codex claude' || printf '%s' "$PROVIDER"); do
    if python3 "$MODEL_SUPPORT" eligible --file "$HEALTH_FILE" \
      --provider "$CANDIDATE" --mode agent >/dev/null 2>&1; then
      PROVIDER_READY=1
      break
    fi
  done
  if [ "$PROVIDER_READY" -ne 1 ]; then
    echo "article-resume: bounded quality repair remains in provider cooldown run=$QUALITY_RUN_ID" >>"$LOG"
    exit 0
  fi
  QUALITY_INVOKING="$(python3 "$QUALITY_REPAIR_CONTROL" invoke \
    --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" \
    --owner-pid "$$" 2>>"$LOG")" || {
    echo "article-resume: bounded quality repair invoke failed closed run=$QUALITY_RUN_ID" >>"$LOG"
    exit 1
  }
  printf '%s\n' "$QUALITY_INVOKING" >>"$LOG"
  INVOKING_PROMPT="$(printf '%s' "$QUALITY_INVOKING" | jq -r '.prompt_path // empty')"
  [ -n "$INVOKING_PROMPT" ] && QUALITY_PROMPT="$INVOKING_PROMPT"
  export ARTICLE_RUN_DIR="$GENERATION_RUN_DIR"
  export ARTICLE_QUALITY_REPAIR_ACTIVE=1
  export ARTICLE_QUALITY_REPAIR_OWNER_PID="$$"
  ARTICLE_MODEL_LOG="$LOG" bash "$ARTICLE_ROOT/runtime/judge-broker.sh" "$GENERATION_RUN_DIR" &
  JUDGE_BROKER_PID=$!
  ARTICLE_RUN_ID="$QUALITY_RUN_ID" ARTICLE_MODEL_LOG="$LOG" \
    "$MODEL_RUNNER" agent --prompt-file "$QUALITY_PROMPT"
  RC=$?
  kill "$JUDGE_BROKER_PID" 2>/dev/null || true
  wait "$JUDGE_BROKER_PID" 2>/dev/null || true
  python3 "$QUALITY_REPAIR_CONTROL" result \
    --run-dir "$GENERATION_RUN_DIR" --ledger "$QUALITY_LEDGER" \
    --return-code "$RC" >>"$LOG" 2>&1 || {
    echo "article-resume: bounded quality repair result failed closed run=$QUALITY_RUN_ID rc=$RC" >>"$LOG"
    exit 1
  }
  echo "article-resume: bounded quality repair run=$QUALITY_RUN_ID rc=$RC" >>"$LOG"
  exit 0
fi

if [ "$ADOPTION_ACTIVE" -eq 1 ] && [ "$PRIORITY_PUBLICATION_READY" -ne 1 ]; then
  echo "article-resume: adopted run remains owned by quality repair run=$GENERATION_RUN_ID" >>"$LOG"
  exit 0
fi

if [ -f "$GENERATION_STATE_PATH" ] \
  && [ ! -f "$GENERATION_RUN_DIR/gates/publication-state.json" ] \
  && [ "$(jq -r '.status // empty' "$GENERATION_STATE_PATH")" = "invoking" ]; then
  python3 "$ARTICLE_ROOT/scripts/article_generation_state.py" \
    --run-dir "$GENERATION_RUN_DIR" \
    --run-id "$GENERATION_RUN_ID" \
    --prompt-file "$GENERATION_RUN_DIR/article-daily-prompt.txt" \
    --ledger "$STATE_DIR/articles.jsonl" \
    recover-orphan --minimum-age-seconds \
    "${ARTICLE_GENERATION_ORPHAN_AGE_SECONDS:-60}" >>"$LOG" 2>&1 || true
fi
START_DECISION="$(python3 "$START_CONTROL" \
  --state-dir "$STATE_DIR" --local-date "$LOCAL_DATE" 2>>"$LOG" || \
  printf '%s' '{"action":"block-incomplete","reason":"start-control-error"}')"
START_ACTION="$(printf '%s' "$START_DECISION" | jq -r '.action // empty')"
START_RUN_ID="$(printf '%s' "$START_DECISION" | jq -r '.run_id // empty')"
if [ "$PRIORITY_PUBLICATION_READY" -ne 1 ] \
  && [ "$START_ACTION" = "new-quality-replacement" ]; then
  [ -n "$START_RUN_ID" ] || {
    echo "article-resume: quality replacement run id missing" >>"$LOG"
    exit 1
  }
  echo "article-resume: handing terminal quality slot to replacement run=$START_RUN_ID" >>"$LOG"
  export ARTICLE_EXPECTED_NEW_RUN_ID="$START_RUN_ID"
  trap - EXIT
  release_publication_lock || exit 1
  exec bash "$ARTICLE_ROOT/article-daily.sh"
fi
if [ "$PRIORITY_PUBLICATION_READY" -ne 1 ] \
  && [ "$START_ACTION" = "resume-generation" ]; then
  GENERATION_RUN_ID="$START_RUN_ID"
  PROVIDER="${ARTICLE_PROVIDER:-codex}"
  HEALTH_FILE="${ARTICLE_PROVIDER_HEALTH:-$STATE_DIR/provider-health.json}"
  PROVIDER_READY=0
  for CANDIDATE in $([ "$PROVIDER" = "auto" ] && printf 'codex claude' || printf '%s' "$PROVIDER"); do
    if python3 "$MODEL_SUPPORT" eligible --file "$HEALTH_FILE" \
      --provider "$CANDIDATE" --mode agent >/dev/null 2>&1; then
      PROVIDER_READY=1
      break
    fi
  done
  if [ "$PROVIDER_READY" -ne 1 ]; then
    echo "article-resume: prepublication generation remains in provider cooldown run=$GENERATION_RUN_ID" >>"$LOG"
    exit 0
  fi
  echo "article-resume: prepublication generation recovery run=$GENERATION_RUN_ID" >>"$LOG"
  export ARTICLE_EXPECTED_RUN_ID="$GENERATION_RUN_ID"
  trap - EXIT
  release_publication_lock || exit 1
  exec bash "$ARTICLE_ROOT/article-daily.sh"
fi

# An unavailable destination is otherwise excluded from pending work forever.
# Re-arm only failure classes whose original blocker can be re-proved as fixed:
# note's locked Python runtime, and Substack's authenticated draft render.
python3 "$ARTICLE_ROOT/scripts/recover-known-unavailable.py" \
  --state-root "$STATE_DIR" >>"$LOG" 2>&1 || \
  echo "article-resume: known unavailable recovery failed closed" >>"$LOG"

# Unknown publisher failures are observations for the Agent repair loop, not a
# terminal exclusion from pending work. The bridge is read-only over run
# receipts and durably deduplicates destination incidents before planning.
python3 "$ARTICLE_ROOT/scripts/writer_unavailable_incident_bridge.py" \
  --state-root "$STATE_DIR" --observed-at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  >>"$LOG" 2>&1 || \
  echo "article-resume: unavailable incident bridge failed closed" >>"$LOG"

PLAN_ARGS=(--state-root "$STATE_DIR")
[ -n "${ARTICLE_NOW:-}" ] && PLAN_ARGS+=(--now "$ARTICLE_NOW")
PLAN="$(python3 "$PLANNER" "${PLAN_ARGS[@]}" 2>>"$LOG")" || {
  echo "article-resume: planner failed closed" >>"$LOG"
  exit 1
}
[ "$(printf '%s' "$PLAN" | jq -r '.status')" = "READY" ] || {
  echo "article-resume: $(printf '%s' "$PLAN" | jq -c '.')" >>"$LOG"
  # Coconala parity: the publication queue is the foreground consumer. Only
  # an empty publication queue may hand this tick to self-heal. Release the
  # publication lock before the bounded model step so repair investigation can
  # never hold a publisher's critical section.
  trap - EXIT
  release_publication_lock || exit 1
  python3 "$ARTICLE_ROOT/scripts/writer_repair_dispatch.py" \
    --state-root "$STATE_DIR" \
    --scripts "$ARTICLE_ROOT/scripts" \
    --registry "$ARTICLE_ROOT/config/repair-runbooks.json" \
    --model-runner "$MODEL_RUNNER" \
    --publication-backlog "0" \
    --observed-at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    >>"$LOG" 2>&1 || \
    echo "article-resume: repair routing failed closed" >>"$LOG"
  exit 0
}
echo "article-resume: publication queue foreground; self-heal deferred run=$(printf '%s' "$PLAN" | jq -r '.run_id // empty')" >>"$LOG"
# Claim and receipt one incident even while publication is READY. The dispatcher
# sees backlog=1 and defers ordinary Terra work, but still performs the durable
# incident claim/runbook decision so a repeatedly failing publication lane cannot
# starve self-heal forever. READY passes --defer-model-always, so even a circuit
# owner waits for the lock-free backlog=0 repair tick.
python3 "$ARTICLE_ROOT/scripts/writer_repair_dispatch.py" \
  --state-root "$STATE_DIR" \
  --scripts "$ARTICLE_ROOT/scripts" \
  --registry "$ARTICLE_ROOT/config/repair-runbooks.json" \
  --model-runner "$MODEL_RUNNER" \
  --publication-backlog "1" \
  --defer-model-always \
  --observed-at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  >>"$LOG" 2>&1 || \
  echo "article-resume: foreground repair routing failed closed" >>"$LOG"
INITIALIZATION_COUNT="$(printf '%s' "$PLAN" | jq '.initialization_pairs | length')"
ELIGIBLE_COUNT="$(printf '%s' "$PLAN" | jq '.eligible_pairs | length')"
FIRST_INITIALIZATION="$(printf '%s' "$PLAN" | jq -r '.initialization_pairs[0] // empty')"
if [ "$INITIALIZATION_COUNT" -gt 0 ] && [ "$ELIGIBLE_COUNT" -gt 0 ]; then
  echo "article-resume: planner mixed initialization and publication work" >>"$LOG"
  exit 1
fi

RUN_ID="$(printf '%s' "$PLAN" | jq -r '.run_id')"
RUN_DIR="$(printf '%s' "$PLAN" | jq -r '.run_dir')"
STATE_PATH="$(printf '%s' "$PLAN" | jq -r '.state_path')"
LEDGER_PATH="$(printf '%s' "$PLAN" | jq -r '.ledger_path')"
case "$RUN_ID" in daily-????-??-??|????????-??????) ;; *) echo "article-resume: invalid run id" >>"$LOG"; exit 1 ;; esac
[ -d "$RUN_DIR" ] && [ -f "$STATE_PATH" ] || exit 1

# X Article staging uses the shared authenticated job-search browser on CDP :9222.
# The Coconala browser on :9223 is a different profile and must never satisfy this
# preflight by accident.
if [ "$FIRST_INITIALIZATION" = "x-article/ja" ] || [ "$FIRST_INITIALIZATION" = "x-article/en" ] \
  || [ "$(printf '%s' "$PLAN" | jq -r '.eligible_pairs[0] // empty')" = "x-article/ja" ]; then
  BROWSER_GUARD="${LIFE_MANAGER_REPO:-$(cd "$ARTICLE_ROOT/../.." && pwd)}/skills/browser/ensure_browser.sh"
  if [ ! -x "$BROWSER_GUARD" ]; then
    echo "article-resume: X browser guard missing at $BROWSER_GUARD" >>"$LOG"
    exit 1
  fi
  WRITER_CDP_ENDPOINT="${WRITER_CDP_URL:-http://127.0.0.1:9222}"
  WRITER_CDP_ENDPOINT_PORT="${WRITER_CDP_PORT:-9222}"
  WRITER_CDP_ENDPOINT_PROFILE="${WRITER_CDP_PROFILE:-$HOME/.cloak/profiles/job-search-daily}"
  WRITER_BROWSER_OWNER_LABEL="${WRITER_BROWSER_LAUNCHD_LABEL:-ai.anicca.job-search-browser}"
  CLOAK_CDP_BASE_URL="$WRITER_CDP_ENDPOINT" \
  CDP_DAILY_DRIVER_PORT="$WRITER_CDP_ENDPOINT_PORT" \
  CDP_DAILY_DRIVER_PROFILE="$WRITER_CDP_ENDPOINT_PROFILE" \
  CLOAK_BROWSER_LAUNCHD_LABEL="$WRITER_BROWSER_OWNER_LABEL" \
  bash "$BROWSER_GUARD" >>"$LOG" 2>&1 || {
    echo "article-resume: X browser preflight failed closed" >>"$LOG"
    exit 1
  }
fi

notify_pending() {
  python3 "$ARTICLE_ROOT/scripts/article-completion-notify.py" \
    --state "$STATE_PATH" --ledger "$LEDGER_PATH" \
    --target "$TELEGRAM_TARGET" \
    --pending --reason "$1" >>"$LOG" 2>&1 || \
    echo "article-resume: pending Telegram report remains queued reason=$1" >>"$LOG"
}

PUBLICATION_CONTRACT="$(python3 "$ARTICLE_ROOT/scripts/publication_contract_resolver.py" \
  --state "$STATE_PATH" --ledger "$LEDGER_PATH" --run-id "$RUN_ID" 2>>"$LOG")" || {
  echo "article-resume: publication contract resolution failed closed run=$RUN_ID" >>"$LOG"
  exit 1
}
case "$PUBLICATION_CONTRACT" in
  legacy-exact8)
    DORMANT_ROUTING_INSTRUCTION="This is legacy exact8; legacy dormant destinations remain eligible and must route their persisted targets through the deterministic publisher."
    DORMANT_ELIGIBILITY_INSTRUCTION="Legacy dormant destinations remain eligible; do not synthesize active-six skip receipts."
    REQUIRED_INTENTS_INSTRUCTION="Require publication-guard.py plan to report all eight valid stable intents."
    INITIALIZATION_CONTRACT_INSTRUCTION="Legacy exact8 initialization must register x-article/en and x-post/ja; x-post/ja target-kind x-post-slot must use the planner's legacy x-post/ja slot owner and publication-guard.py registration/plan guards."
    DORMANT_RECEIPT_INSTRUCTION="Legacy exact8 has no dormant skip receipts; x-post/ja remains real pending publication work."
    COMPLETION_CONTRACT_INSTRUCTION="Do not select or claim a topic; research, write, revise, regenerate, restage, create a run, replace a target, change an artifact, or send a success receipt unless article-run-complete.py proves the persisted legacy-exact8 contract."
    RECOVERY_SCOPE_INSTRUCTION="If any identity, hash, safety, remote state, or slot is ambiguous outside the legacy-exact8 recovery_pairs contract above, stop without a public create action."
    REPAIR_SCOPE_INSTRUCTION="An eligible pair whose persisted status is repair-required is NEVER a create. Use only its fixed same-ID command: note/ja = python3 $ARTICLE_ROOT/scripts/note-publish/note_inplace_repair.py; devto/en = python3 $ARTICLE_ROOT/scripts/devto-publish/devto.py repair; substack/ja or substack/en = python3 $ARTICLE_ROOT/scripts/substack-publish/substack_inplace_repair.py --pair PAIR; x-article/ja = python3 $ARTICLE_ROOT/scripts/x-publish/x_inplace_repair.py --pair x-article/ja. For x-post/ja, a strong remote reconcile is sufficient because its media contract is empty. Refuse every repair-required pair not covered by this list."
    X_POST_COMMAND_INSTRUCTION="The canonical X Post command is python3 $ARTICLE_ROOT/scripts/x-post/publish.py go; it owns the durable pre-effect timeline fence and must not be replaced with an ad-hoc browser click."
    ;;
  active-four)
    DORMANT_ROUTING_INSTRUCTION="Current active-four: zenn-article/ja, devto/en, x-article/en, and x-post/ja receive explicit durable dormant-destination skip receipts and never receive publication intents."
    DORMANT_ELIGIBILITY_INSTRUCTION="Dormant zenn-article/ja, devto/en, x-article/en, x-post/ja are never eligible. Never publish, repair, or stage zenn-article/ja or devto/en."
    REQUIRED_INTENTS_INSTRUCTION="Require publication-guard.py plan to report all four active valid stable intents plus four durable dormant skip receipts."
    INITIALIZATION_CONTRACT_INSTRUCTION="Active-four initialization must register every missing_dormant_skip_pairs entry through publication-guard.py register-dormant-skip before or alongside the four active target registrations; register only note/ja, substack/ja, substack/en, and x-article/ja, and do not publish any pair in this initialization tick."
    DORMANT_RECEIPT_INSTRUCTION="The four dormant skip receipts have slo=not-applicable, never breach an SLO, and never enter pending, recovery, or publication work."
    COMPLETION_CONTRACT_INSTRUCTION="Do not select or claim a topic; research, write, revise, regenerate, restage, create a run, replace a target, change an artifact, or send a success receipt unless article-run-complete.py proves the persisted active-four contract."
    RECOVERY_SCOPE_INSTRUCTION="If any identity, hash, safety, remote state, or slot is ambiguous outside the active-four recovery_pairs contract above, stop without a public create action."
    REPAIR_SCOPE_INSTRUCTION="An eligible pair whose persisted status is repair-required is NEVER a create. Use only its fixed same-ID command: note/ja = python3 $ARTICLE_ROOT/scripts/note-publish/note_inplace_repair.py; substack/ja or substack/en = python3 $ARTICLE_ROOT/scripts/substack-publish/substack_inplace_repair.py --pair PAIR; x-article/ja = python3 $ARTICLE_ROOT/scripts/x-publish/x_inplace_repair.py --pair x-article/ja. Refuse every repair-required zenn-article/ja or devto/en and every pair not covered by this list."
    X_POST_COMMAND_INSTRUCTION="Never publish, repair, or stage dormant x-post/ja; its explicit skip receipt is not an eligible command."
    ;;
  *)
    echo "article-resume: unsupported publication contract=$PUBLICATION_CONTRACT" >>"$LOG"
    exit 1
    ;;
esac

# A current active-four run must persist every missing dormant skip receipt before
# any active target initialization. This is a publication-free state transition.
MISSING_DORMANT_SKIP_PAIRS="$(printf '%s' "$PLAN" | jq -r '.missing_dormant_skip_pairs[]?')"
if [ "$PUBLICATION_CONTRACT" = "active-four" ] && [ -n "$MISSING_DORMANT_SKIP_PAIRS" ]; then
  export ARTICLE_RUN_DIR="$RUN_DIR"
  export ARTICLE_PUBLICATION_STATE="$STATE_PATH"
  export ARTICLE_LEDGER="$LEDGER_PATH"
  export ARTICLE_AUTOPUBLISH=1
  while IFS= read -r DORMANT_PAIR; do
    [ -n "$DORMANT_PAIR" ] || continue
    python3 "$ARTICLE_ROOT/scripts/publication-guard.py" \
      register-dormant-skip --pair "$DORMANT_PAIR" \
      --reason "dormant-destination" >>"$LOG" 2>&1 || {
        echo "article-resume: dormant skip registration failed pair=$DORMANT_PAIR" >>"$LOG"
        exit 1
      }
  done <<< "$MISSING_DORMANT_SKIP_PAIRS"
fi

# The self-owned paid publication is an adjunct to active-four. It runs under
# its own same-run lock but stays synchronous while the parent owner fence is
# held; a background child could outlive this shell and publish after the fence
# was released. Its failure is recorded and does not change the active-four
# recovery scope.
if [ -n "${ARTICLE_SELF_OWNED_LANDING_ROOT:-}" ] \
  && [ -n "${ARTICLE_SELF_OWNED_REMOTE:-}" ] \
  && [ -n "${ARTICLE_SELF_OWNED_BRANCH:-}" ]; then
  (
    SELF_OWNED_LOCK="$RUN_DIR/gates/self-owned-worker.lock"
    if ! mkdir "$SELF_OWNED_LOCK" 2>/dev/null; then
      exit 0
    fi
    trap 'rmdir "$SELF_OWNED_LOCK" 2>/dev/null || true' EXIT
    python3 "$ARTICLE_ROOT/scripts/self_owned_article.py" resume \
      --publication-state "$STATE_PATH" \
      --ledger "$LEDGER_PATH" \
      --landing-root "$ARTICLE_SELF_OWNED_LANDING_ROOT" \
      --remote "$ARTICLE_SELF_OWNED_REMOTE" \
      --branch "$ARTICLE_SELF_OWNED_BRANCH" \
      --base-url "${ARTICLE_SELF_OWNED_BASE_URL:-https://aniccaai.com}"
  ) >>"$LOG" 2>&1 </dev/null || \
    echo "article-resume: self-owned worker remains pending" >>"$LOG"
fi

# A recognized note ambiguity is a deterministic state transition, not a
# writing decision. Prove its authenticated same-key live-media shape and
# convert it to repair-required in one publication-free tick. The next tick
# runs only the protected in-place repair.
if [ "$(printf '%s' "$PLAN" | jq -r '.recovery_pairs[0] // empty')" = "note/ja" ] \
  && [ "$(jq -r '.pairs["note/ja"].status // empty' "$STATE_PATH")" = "ambiguous" ]; then
  export ARTICLE_RUN_DIR="$RUN_DIR"
  export ARTICLE_PUBLICATION_STATE="$STATE_PATH"
  export ARTICLE_LEDGER="$LEDGER_PATH"
  export ARTICLE_AUTOPUBLISH=1
  export ARTICLE_PUBLISH_PAIR="note/ja"
  python3 "$ARTICLE_ROOT/scripts/resume_failure_circuit.py" run \
    --circuit "$RUN_DIR/gates/resume-failure-circuit.json" \
    --state "$STATE_PATH" \
    --code-file "$ARTICLE_ROOT/scripts/article-resume-pending.sh" \
    --code-file "$ARTICLE_ROOT/scripts/publication-guard.py" \
    --code-file "$ARTICLE_ROOT/scripts/publication_remote.py" \
    --code-file "$ARTICLE_ROOT/scripts/publication_resume.py" \
    --pair "note/ja" \
    --threshold "${ARTICLE_RESUME_FAILURE_THRESHOLD:-2}" \
    --log "$LOG" \
    -- python3 "$ARTICLE_ROOT/scripts/publication-guard.py" \
      recover-ambiguous --pair "note/ja" >>"$LOG" 2>&1
  RC=$?
  echo "article-resume: run=$RUN_ID rc=$RC deterministic=recover-note/ja" >>"$LOG"
  exit "$RC"
fi

# A missing managed target has no editorial decision left: STEP 5 already
# persisted its exact argv/env in the preflighted dispatch manifest. Execute
# supported deterministic rows once and require a durable target before
# exiting the publication-free initialization tick.
if [ "$INITIALIZATION_COUNT" -eq 1 ] \
  && [ "$ELIGIBLE_COUNT" -eq 0 ] \
  && { [ "$FIRST_INITIALIZATION" = "note/ja" ] \
    || { [ "$PUBLICATION_CONTRACT" = "legacy-exact8" ] \
      && [ "$FIRST_INITIALIZATION" = "devto/en" ]; }; }; then
  ARTICLE_MODEL_LOG="$LOG" \
    bash "$ARTICLE_ROOT/runtime/judge-broker.sh" "$RUN_DIR" &
  INITIALIZATION_BROKER_PID=$!
  trap 'kill "$INITIALIZATION_BROKER_PID" 2>/dev/null; release_publication_lock' EXIT
  python3 "$ARTICLE_ROOT/scripts/execute-initialization-pair.py" \
    --pair "$FIRST_INITIALIZATION" --run-dir "$RUN_DIR" --state "$STATE_PATH" \
    --ledger "$LEDGER_PATH" >>"$LOG" 2>&1
  RC=$?
  kill "$INITIALIZATION_BROKER_PID" 2>/dev/null || true
  wait "$INITIALIZATION_BROKER_PID" 2>/dev/null || true
  echo "article-resume: run=$RUN_ID rc=$RC deterministic=initialize-$FIRST_INITIALIZATION" >>"$LOG"
  exit "$RC"
fi

# The first money-bearing note intent is a fully specified operation: immutable
# eyecatch, executable ¥500 policy, guarded publish, public API readback, and
# receipt reconciliation. Run that narrow contract directly instead of giving
# a general model the frozen run prompt (which may predate the current money
# policy) plus several unrelated eligible destinations.
if [ "$INITIALIZATION_COUNT" -eq 0 ] \
  && [ "$ELIGIBLE_COUNT" -gt 0 ] \
  && [ "$(printf '%s' "$PLAN" | jq -r '.eligible_pairs[0]')" = "note/ja" ]; then
  export ARTICLE_RUN_DIR="$RUN_DIR"
  export ARTICLE_PUBLICATION_STATE="$STATE_PATH"
  export ARTICLE_LEDGER="$LEDGER_PATH"
  export ARTICLE_AUTOPUBLISH=1
  export ARTICLE_PUBLISH_PAIR="note/ja"
  NOTE_STATUS="$(jq -r '.pairs["note/ja"].status // empty' "$STATE_PATH")"
  NOTE_CODE_ARGS=()
  if [ "$NOTE_STATUS" = "repair-required" ]; then
    NOTE_MCP_DIR="${NOTE_MCP_DIR:-$WRITER_RUNTIME_HOME/external/note-mcp}"
    bash "$ARTICLE_ROOT/scripts/ensure-note-mcp-runtime.sh" \
      "$NOTE_MCP_DIR" >>"$LOG" 2>&1 || {
      echo "article-resume: note-mcp runtime restore failed closed" >>"$LOG"
      exit 1
    }
    export NOTE_MCP_DIR
    export NOTE_MCP_SRC="$NOTE_MCP_DIR/src"
    NOTE_COMMAND=(
      "$NOTE_MCP_DIR/.venv/bin/python"
      "$ARTICLE_ROOT/scripts/note-publish/note_inplace_repair.py"
    )
    NOTE_CODE_ARGS=(
      --code-file "$ARTICLE_ROOT/scripts/note-publish/note_inplace_repair.py"
      --code-file "$ARTICLE_ROOT/scripts/ensure-note-mcp-runtime.sh"
    )
  elif [ "$NOTE_STATUS" = "intent" ]; then
    NOTE_COMMAND=(python3 "$ARTICLE_ROOT/scripts/publish-note-managed.py")
    NOTE_CODE_ARGS=(
      --code-file "$ARTICLE_ROOT/scripts/publish-note-managed.py"
      --code-file "$ARTICLE_ROOT/scripts/note-publish/set-eyecatch-draft.py"
      --code-file "$ARTICLE_ROOT/scripts/note-publish/set-eyecatch-api.py"
      --code-file "$ARTICLE_ROOT/scripts/note-publish/publish-paid.py"
      --code-file "$ARTICLE_ROOT/scripts/ensure-note-mcp-runtime.sh"
    )
  else
    echo "article-resume: note deterministic dispatch refused status=$NOTE_STATUS" >>"$LOG"
    exit 1
  fi
  python3 "$ARTICLE_ROOT/scripts/resume_failure_circuit.py" run \
    --circuit "$RUN_DIR/gates/resume-failure-circuit.json" \
    --state "$STATE_PATH" \
    "${NOTE_CODE_ARGS[@]}" \
    --pair "note/ja" \
    --threshold "${ARTICLE_RESUME_FAILURE_THRESHOLD:-2}" \
    --log "$LOG" \
    -- "${NOTE_COMMAND[@]}" >>"$LOG" 2>&1
  RC=$?
  python3 "$ARTICLE_ROOT/scripts/article-completion-notify.py" \
    --state "$STATE_PATH" --ledger "$LEDGER_PATH" \
    --target "$TELEGRAM_TARGET" >>"$LOG" 2>&1 || \
    echo "article-resume: $PUBLICATION_CONTRACT completion notification remains pending" >>"$LOG"
  notify_pending "Noteの公開と読み戻しが未完了です"
  echo "article-resume: run=$RUN_ID rc=$RC deterministic=note/ja" >>"$LOG"
  exit "$RC"
fi

FIRST_ELIGIBLE="$(printf '%s' "$PLAN" | jq -r '.eligible_pairs[0] // empty')"
if [ "$INITIALIZATION_COUNT" -eq 0 ] \
  && [ "$ELIGIBLE_COUNT" -gt 0 ] \
  && { [ "$FIRST_ELIGIBLE" = "substack/ja" ] || [ "$FIRST_ELIGIBLE" = "substack/en" ]; }; then
  export ARTICLE_RUN_DIR="$RUN_DIR" ARTICLE_PUBLICATION_STATE="$STATE_PATH"
  export ARTICLE_LEDGER="$LEDGER_PATH" ARTICLE_AUTOPUBLISH=1
  export ARTICLE_PUBLISH_PAIR="$FIRST_ELIGIBLE"
  SUBSTACK_CODE_ARGS=(
    --code-file "$ARTICLE_ROOT/scripts/publish-substack-managed.py"
    --code-file "$ARTICLE_ROOT/scripts/substack-publish/substack_refresh_intent.py"
    --code-file "$ARTICLE_ROOT/scripts/substack-publish/substack_paid_payload.py"
    --code-file "$ARTICLE_ROOT/scripts/substack-publish/substack_inplace_repair.py"
    --code-file "$ARTICLE_ROOT/scripts/_shared/publish-substack-mermaid.sh"
    --code-file "$ARTICLE_ROOT/scripts/publication-guard.py"
    --code-file "$ARTICLE_ROOT/scripts/publication_remote.py"
    --code-file "$ARTICLE_ROOT/scripts/publication_resume.py"
    --code-file "$ARTICLE_ROOT/scripts/pii-gate.py"
    --code-file "$ARTICLE_ROOT/scripts/substack-publish/verify-preview.py"
  )
  SUBSTACK_COMMAND=(python3 "$ARTICLE_ROOT/scripts/publish-substack-managed.py")
  python3 "$ARTICLE_ROOT/scripts/resume_failure_circuit.py" run \
    --circuit "$RUN_DIR/gates/resume-failure-circuit.json" \
    --state "$STATE_PATH" \
    "${SUBSTACK_CODE_ARGS[@]}" \
    --pair "$FIRST_ELIGIBLE" \
    --threshold "${ARTICLE_RESUME_FAILURE_THRESHOLD:-2}" \
    --log "$LOG" \
    -- "${SUBSTACK_COMMAND[@]}" >>"$LOG" 2>&1
  RC=$?
  python3 "$ARTICLE_ROOT/scripts/article-completion-notify.py" \
    --state "$STATE_PATH" --ledger "$LEDGER_PATH" \
    --target "$TELEGRAM_TARGET" >>"$LOG" 2>&1 || \
    echo "article-resume: $PUBLICATION_CONTRACT completion notification remains pending" >>"$LOG"
  notify_pending "Substackの公開と読み戻しが未完了です"
  echo "article-resume: run=$RUN_ID rc=$RC deterministic=$FIRST_ELIGIBLE" >>"$LOG"
  exit "$RC"
fi

# X Article publication and same-ID repair are deterministic and fully
# guarded. Do not boot a general-purpose model merely to invoke the fixed
# publisher, especially during EN's narrow platform-clock window.
if [ "$INITIALIZATION_COUNT" -eq 0 ] \
  && [ "$ELIGIBLE_COUNT" -gt 0 ] \
  && { [ "$FIRST_ELIGIBLE" = "x-article/ja" ] \
    || { [ "$PUBLICATION_CONTRACT" = "legacy-exact8" ] \
      && [ "$FIRST_ELIGIBLE" = "x-article/en" ]; }; }; then
  export ARTICLE_RUN_DIR="$RUN_DIR"
  export ARTICLE_PUBLICATION_STATE="$STATE_PATH"
  export ARTICLE_LEDGER="$LEDGER_PATH"
  export ARTICLE_AUTOPUBLISH=1
  export ARTICLE_PUBLISH_PAIR="$FIRST_ELIGIBLE"
  python3 "$ARTICLE_ROOT/scripts/x-publish/x_inplace_repair.py" \
    --pair "$FIRST_ELIGIBLE" >>"$LOG" 2>&1
  RC=$?
  python3 "$ARTICLE_ROOT/scripts/article-completion-notify.py" \
    --state "$STATE_PATH" --ledger "$LEDGER_PATH" \
    --target "$TELEGRAM_TARGET" >>"$LOG" 2>&1 || \
    echo "article-resume: $PUBLICATION_CONTRACT completion notification remains pending" >>"$LOG"
  notify_pending "X記事の公開と読み戻しが未完了です"
  echo "article-resume: run=$RUN_ID rc=$RC deterministic=$FIRST_ELIGIBLE" >>"$LOG"
  exit "$RC"
fi

# Dev.to already has one authenticated numeric draft ID at this boundary.
# Publishing that exact ID is a fixed guarded API operation, so a general
# model adds latency and another failure surface without making a decision.
if [ "$INITIALIZATION_COUNT" -eq 0 ] \
  && [ "$ELIGIBLE_COUNT" -eq 1 ] \
  && [ "$PUBLICATION_CONTRACT" = "legacy-exact8" ] \
  && [ "$FIRST_ELIGIBLE" = "devto/en" ]; then
  export ARTICLE_RUN_DIR="$RUN_DIR"
  export ARTICLE_PUBLICATION_STATE="$STATE_PATH"
  export ARTICLE_LEDGER="$LEDGER_PATH"
  export ARTICLE_AUTOPUBLISH=1
  export ARTICLE_PUBLISH_PAIR="devto/en"
  DEVTO_TARGET="$(jq -r '.pairs["devto/en"].target // empty' "$STATE_PATH")"
  case "$DEVTO_TARGET" in
    ''|*[!0-9]*) echo "article-resume: invalid Dev.to target" >>"$LOG"; exit 1 ;;
  esac
  python3 "$ARTICLE_ROOT/scripts/devto-publish/devto.py" go "$DEVTO_TARGET" >>"$LOG" 2>&1
  RC=$?
  python3 "$ARTICLE_ROOT/scripts/article-completion-notify.py" \
    --state "$STATE_PATH" --ledger "$LEDGER_PATH" \
    --target "$TELEGRAM_TARGET" >>"$LOG" 2>&1 || \
    echo "article-resume: $PUBLICATION_CONTRACT completion notification remains pending" >>"$LOG"
  notify_pending "Dev.toの公開と読み戻しが未完了です"
  echo "article-resume: run=$RUN_ID rc=$RC deterministic=devto/en" >>"$LOG"
  exit "$RC"
fi

# X Post also has a single canonical, fully guarded command. Once the planner
# owns today's durable slot, a general model adds latency but no decision.
if [ "$INITIALIZATION_COUNT" -eq 0 ] \
  && [ "$ELIGIBLE_COUNT" -eq 1 ] \
  && [ "$PUBLICATION_CONTRACT" = "legacy-exact8" ] \
  && [ "$(printf '%s' "$PLAN" | jq -r '.eligible_pairs[0]')" = "x-post/ja" ]; then
  export ARTICLE_RUN_DIR="$RUN_DIR"
  export ARTICLE_PUBLICATION_STATE="$STATE_PATH"
  export ARTICLE_LEDGER="$LEDGER_PATH"
  export ARTICLE_AUTOPUBLISH=1
  export ARTICLE_PUBLISH_PAIR="x-post/ja"
  python3 "$ARTICLE_ROOT/scripts/x-post/publish.py" go >>"$LOG" 2>&1
  RC=$?
  python3 "$ARTICLE_ROOT/scripts/article-completion-notify.py" \
    --state "$STATE_PATH" --ledger "$LEDGER_PATH" \
    --target "$TELEGRAM_TARGET" >>"$LOG" 2>&1 || \
    echo "article-resume: $PUBLICATION_CONTRACT completion notification remains pending" >>"$LOG"
  notify_pending "X短文の公開と読み戻しが未完了です"
  echo "article-resume: run=$RUN_ID rc=$RC deterministic=x-post/ja" >>"$LOG"
  exit "$RC"
fi

PROMPT_FILE="$RUN_DIR/gates/pending-resume-$(date -u +%Y%m%dT%H%M%SZ).txt"
printf '%s\n' "RESUME ONLY immutable $PUBLICATION_CONTRACT publication run $RUN_ID.
Export ARTICLE_RUN_DIR=$RUN_DIR, ARTICLE_PUBLICATION_STATE=$STATE_PATH, ARTICLE_LEDGER=$LEDGER_PATH, and ARTICLE_AUTOPUBLISH=1 for every managed publisher.
Read $STATE_PATH, the original $RUN_DIR/article-daily-prompt.txt publication steps, and saved artifacts in $RUN_DIR.
This worker plan is authoritative: $PLAN
When initialization_pairs is non-empty, this is a crash-truncated target-registration recovery tick. Existing pair targets are immutable: never restage, replace, update, or re-register them. For each initialization pair only, first reconcile whether the failed foreground already left a same-run draft/target whose content matches the immutable state artifact; reuse that exact target when proven. Otherwise run only the original managed STEP 5 staging path for that missing pair and register its returned stable identity through publication-guard.py register-intent. $DORMANT_ROUTING_INSTRUCTION $INITIALIZATION_CONTRACT_INSTRUCTION $REQUIRED_INTENTS_INSTRUCTION; do not publish any pair in this initialization tick. Exit and let the next 300-second tick own publication.
Execute ONLY eligible_pairs, in listed order. Reconcile the persisted stable target remotely before every create/publish action. Already-live pairs, waiting pairs, and pairs absent from eligible_pairs are forbidden.
An eligible x-article/ja listed in recovery_pairs is a bounded same-ID recovery, not create permission: run only x_inplace_repair.py, whose first guard call must convert authenticated not-live evidence back to intent or an exactly identified live media/final-CTA gap to repair-required. Any other ambiguity remains frozen.
${REPAIR_SCOPE_INSTRUCTION}
For x-article/ja, when eligible, update and publish only its persisted saved edit URL with python3 $ARTICLE_ROOT/scripts/x-publish/x_inplace_repair.py --pair x-article/ja; never open the X new-article composer. $DORMANT_ELIGIBILITY_INSTRUCTION
For substack pairs, every public image must be the byte-identical immutable run media (headline-image.png and each body asset uploaded unchanged); never render Mermaid or substitute any alternative figure — a rendered or re-encoded diagram fails the receipt and strands the pair.
$COMPLETION_CONTRACT_INSTRUCTION $DORMANT_RECEIPT_INSTRUCTION
$X_POST_COMMAND_INSTRUCTION
$RECOVERY_SCOPE_INSTRUCTION
Do not send Telegram messages from the model. The deterministic wrapper sends the only progress or completion report after it reads the persisted state. State what was attempted and what remains unconfirmed in the run log; never add a harness prefix or claim a public URL without publisher-native readback." >"$PROMPT_FILE"

# Judge broker: serve nested judge/vision requests from the sandboxed agent
# through the same model boundary, outside the sandbox.
ARTICLE_MODEL_LOG="$LOG" bash "$ARTICLE_ROOT/runtime/judge-broker.sh" "$RUN_DIR" &
JUDGE_BROKER_PID=$!
trap 'kill "$JUDGE_BROKER_PID" 2>/dev/null; release_publication_lock' EXIT

ARTICLE_RUN_ID="$RUN_ID" ARTICLE_MODEL_LOG="$LOG" \
  "$MODEL_RUNNER" agent --prompt-file "$PROMPT_FILE"
RC=$?
kill "$JUDGE_BROKER_PID" 2>/dev/null || true
python3 "$ARTICLE_ROOT/scripts/article-completion-notify.py" \
  --state "$STATE_PATH" --ledger "$LEDGER_PATH" \
  --target "$TELEGRAM_TARGET" >>"$LOG" 2>&1 || \
  echo "article-resume: $PUBLICATION_CONTRACT completion notification remains pending" >>"$LOG"
notify_pending "公開処理が未完了です"
echo "article-resume: run=$RUN_ID rc=$RC eligible=$(printf '%s' "$PLAN" | jq -c '.eligible_pairs')" >>"$LOG"
exit "$RC"
