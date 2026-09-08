#!/usr/bin/env bash
# article-daily.sh — DETERMINISTIC daily trigger for Writer Agent
# (Dais 2026-07-12: same root cause as capafy/life-manager on 2026-07-12 — a self-registered
# self-registered recurring-scheduler tool call never verifiably fires on its own; process-alive
# != daily output. Fix = the proven connector/capafy/life-manager pattern: launchd
# StartCalendarInterval calls one bounded foreground model pass directly. NO timeout wraps the call —
# capafy died at rc=124 mid-publish, life-manager died at rc=124 having posted nothing; this
# loop runs until the work is done. This file never asks the agent to self-register a
# scheduler — launchd is the ONLY scheduler.
# Reporting uses the repository-owned Telegram sender.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
set -uo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/writer-runtime-env.sh
source "$SCRIPT_DIR/scripts/writer-runtime-env.sh"
# Runtime topic stages remain under the mutable state root: state/topics/queue/,
# state/topics/in-progress/, and state/topics/done/.
LOG="${ARTICLE_DAILY_LOG:-$WRITER_LOG_DIR/article-daily.log}"
mkdir -p "$(dirname "$LOG")"
# The Telegram target is supplied by the Life Manager environment.
PUBLICATION_PAUSE_FILE="${ARTICLE_PUBLICATION_PAUSE_FILE:-$STATE_DIR/.publication-paused}"
if [ -f "$PUBLICATION_PAUSE_FILE" ]; then
  echo "article-daily: publication paused file=$PUBLICATION_PAUSE_FILE at=$(date -u '+%FT%TZ')" >>"$LOG"
  exit 0
fi
PUBLICATION_PAUSE_SNAPSHOT="absent"
echo "article-daily: publication pause current=absent at=$(date -u '+%FT%TZ') file=$PUBLICATION_PAUSE_FILE" >>"$LOG"
if [ "${ARTICLE_OWNER_FENCE_ACTIVE:-0}" != "1" ]; then
  OWNER_FENCE_DIR="${ARTICLE_OWNER_FENCE_DIR:-$HOME/.local/state/life-manager/writer/owner-fence}"
  export ARTICLE_OWNER_FENCE_DIR
  exec python3 "$ARTICLE_ROOT/scripts/writer_owner_fence.py" run \
    --fence-dir "$OWNER_FENCE_DIR" --owner article-daily \
    --root "$ARTICLE_ROOT" --state "$STATE_DIR" \
    --run-id "${ARTICLE_EXPECTED_RUN_ID:-daily-$(TZ=Asia/Tokyo date +%F)}" \
    -- "$0" "$@"
fi
TELEGRAM_TARGET_ID="${TELEGRAM_TARGET_ID:-8547730585}"
ARTICLE_PROVIDER_COOLDOWN_SECONDS="300"
ARTICLE_PRODUCT_ID="${ARTICLE_PRODUCT_ID:-anicca}"
ARTICLE_PRODUCT_LANDING_URL="${ARTICLE_PRODUCT_LANDING_URL:-https://aniccaai.com/}"
ARTICLE_PUBLICATION_POLICY="${ARTICLE_PUBLICATION_POLICY:-continuous}"
export ARTICLE_PROVIDER_COOLDOWN_SECONDS
export ARTICLE_PRODUCT_ID ARTICLE_PRODUCT_LANDING_URL
export ARTICLE_PUBLICATION_POLICY
export TELEGRAM_ALERT_CHAT_ID="$TELEGRAM_TARGET_ID"
# spec #22 self-heal L2: telegram_notify() is the shared out-of-band alert path 211 other
# crons already use (see the script's own header) -- reused here rather than re-implementing
# a second Telegram transport.
. "$LIFE_MANAGER_REPO/skills/_shared/scripts/telegram-notify.sh" 2>/dev/null || true
echo "=== article-daily run $(date '+%F %T %Z') ===" >>"$LOG"

# DISK PREFLIGHT (spec writer-loop-spec.md #13.1 item 5 / #13.5): runs at wrapper start, before
# the exclusive lock, before RUN_DIR, before any model invocation below -- a pass that can run
# for hours writes this LOG, a whole state/runs/<ts>/ record tree, gate JSONs and Chromium
# screenshots, on top of elsewhere-managed .backups tarballs and cloned repos on the same volume;
# with no floor check, / filling mid-pass means every one of those writes silently truncates
# instead of failing loud. This is a plain host disk check the wrapper makes on its own -- never
# something the LLM inside the pass decides or can skip.
# Life Manager's shared disk admission defaults to 524288 KiB. Keep the
# in-process check identical so direct owner wakes and supervised lanes agree.
CANONICAL_DISK_HEADROOM_KIB=524288
GIG_DISK_HEADROOM_KIB="${GIG_DISK_HEADROOM_KIB:-$CANONICAL_DISK_HEADROOM_KIB}"
export GIG_DISK_HEADROOM_KIB
case "$GIG_DISK_HEADROOM_KIB" in
  ''|*[!0-9]*|0)
    echo "=== article-daily disk floor configuration invalid ===" >>"$LOG"
    exit 1
    ;;
esac
DISK_LOW_THRESHOLD_BYTES="$(python3 "$ARTICLE_ROOT/scripts/writer_capacity_floor.py" --state-dir "$STATE_DIR")" || {
  echo "=== article-daily capacity receipt invalid ===" >>"$LOG"
  exit 1
}
case "$DISK_LOW_THRESHOLD_BYTES" in
  ''|*[!0-9]*|0)
    echo "=== article-daily disk floor configuration invalid ===" >>"$LOG"
    exit 1
    ;;
esac
if [ "$GIG_DISK_HEADROOM_KIB" -lt "$CANONICAL_DISK_HEADROOM_KIB" ] \
  || [ "$DISK_LOW_THRESHOLD_BYTES" -lt "$((CANONICAL_DISK_HEADROOM_KIB * 1024))" ]; then
  echo "=== article-daily disk floor configuration below canonical minimum ===" >>"$LOG"
  exit 1
fi

disk_free_bytes() {
  # macOS/APFS: -P forces single-line POSIX output regardless of filesystem-name length; -k
  # reports 1024-byte blocks so the Available column (field 4) converts to bytes with a plain
  # multiply. No GNU-only `-B`/`--output` flag here -- macOS ships BSD df, which has neither.
  local free_kb
  free_kb="$(df -Pk / 2>/dev/null | awk 'NR==2{print $4}')"
  echo $(( ${free_kb:-0} * 1024 ))
}

disk_preflight() {
  local before_bytes after_bytes freed_bytes actions
  before_bytes="$(disk_free_bytes)"
  if [ "${before_bytes:-0}" -ge "$DISK_LOW_THRESHOLD_BYTES" ]; then
    echo "=== article-daily disk-preflight: free=${before_bytes}bytes (>= ${DISK_LOW_THRESHOLD_BYTES}bytes threshold), no cleanup needed $(date '+%F %T %Z') ===" >>"$LOG"
    return 0
  fi

  actions=""

  # $HOME/.cache/anicca-clones/: clear its contents, keep the directory itself. mindepth 1
  # maxdepth 1 + `-exec rm -rf {} +` never descends into or globs anything outside this one root.
  local clones_dir="$HOME/.cache/anicca-clones"
  if [ -d "$clones_dir" ]; then
    local clones_count
    clones_count=$(find "$clones_dir" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l | tr -d ' ')
    if [ "${clones_count:-0}" -gt 0 ]; then
      find "$clones_dir" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + 2>/dev/null
      actions="${actions}cleared ${clones_count} entries under $clones_dir; "
    fi
  fi

  after_bytes="$(disk_free_bytes)"
  freed_bytes=$(( ${after_bytes:-0} - ${before_bytes:-0} ))
  [ "$freed_bytes" -lt 0 ] && freed_bytes=0
  echo "=== article-daily disk-preflight: LOW SPACE before=${before_bytes}bytes actions=[${actions:-none}] freed=${freed_bytes}bytes after=${after_bytes}bytes $(date '+%F %T %Z') ===" >>"$LOG"
}

disk_preflight

# Cleanup is best-effort and can free less than the writer needs. Re-measure
# before creating a run or invoking a model; the creator must share the same
# fail-closed boundary as article-resume-pending.sh and disk_admission.py.
POST_PREFLIGHT_FREE_BYTES="$(disk_free_bytes)"
if [ "${POST_PREFLIGHT_FREE_BYTES:-0}" -lt "$DISK_LOW_THRESHOLD_BYTES" ]; then
  echo "=== article-daily disk floor blocked after preflight free=${POST_PREFLIGHT_FREE_BYTES}bytes required=${DISK_LOW_THRESHOLD_BYTES}bytes $(date '+%F %T %Z') ===" >>"$LOG"
  telegram_notify "Writer blocked: disk floor remains below ${DISK_LOW_THRESHOLD_BYTES} bytes (${POST_PREFLIGHT_FREE_BYTES} bytes free)" || true
  exit 1
fi

# EXCLUSIVE LOCK (copied from capafy-autopublish/scripts/daily_loop.sh, 2026-07-12): every loop
# that drives the shared daily-driver browser (CDP :9222) must hold one — capafy ran without a
# lock and two schedulers raced on the same tab 5x in 90 minutes, each seeing the other's
# half-edited DOM and dying on max-turns. mkdir is atomic on every POSIX fs and needs no extra
# binary (macOS ships no `flock` CLI); a second concurrent invocation exits immediately instead
# of silently corrupting the first.
mkdir -p "$STATE_DIR"
RULE_SCAN_RECEIPT="$STATE_DIR/rule-conflicts-latest.json"
RULE_GATE_RECEIPT="$STATE_DIR/contradiction-gate-latest.json"
python3 "$ARTICLE_ROOT/scripts/rule_conflicts.py" \
  scan --json >"$RULE_SCAN_RECEIPT" 2>>"$LOG" &&
python3 "$ARTICLE_ROOT/scripts/contradiction_gate.py" \
  --scan-json "$RULE_SCAN_RECEIPT" --out "$RULE_GATE_RECEIPT" >>"$LOG" 2>&1 || {
  echo "=== article-daily contradiction gate blocked generation ===" >>"$LOG"
  telegram_notify "Writer blocked: critical rule conflict. receipt=$RULE_GATE_RECEIPT" || true
  exit 1
}
python3 "$ARTICLE_ROOT/scripts/topic_state.py" \
  --skill-dir "$ARTICLE_ROOT" >>"$LOG" 2>&1 || {
  echo "=== article-daily topic-state initialization failed closed ===" >>"$LOG"
  exit 1
}
ARTICLE_PROVIDER_HEALTH="${ARTICLE_PROVIDER_HEALTH:-$STATE_DIR/provider-health.json}"
ARTICLE_MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-$ARTICLE_ROOT/runtime/model-runner.sh}"
ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS="${ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS:-900}"
case "$ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS" in
  ''|*[!0-9]*|0)
    echo "article-daily: invalid ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS=$ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS" >>"$LOG"
    exit 1
    ;;
esac
ARTICLE_SKILL_DIR="$ARTICLE_ROOT"
export ARTICLE_PROVIDER_HEALTH ARTICLE_MODEL_RUNNER ARTICLE_SKILL_DIR
# Short acquisition/recovery mutex.  It is released immediately after the
# canonical CDP lock is acquired; it is never held for the model/publication pass.
RECOVERY_LOCK_DIR="$STATE_DIR/.article-daily.recovery.lockdir"
RECOVERY_LOCK_OWNER="$RECOVERY_LOCK_DIR/owner.token"
LOCK_DIR="$STATE_DIR/.article-daily.lockdir"
RECOVERY_LOCK_TOKEN="article-daily-$$-${RANDOM:-0}-$(date +%s)"
process_start_token() {
  ps -p "$1" -o lstart= 2>/dev/null | sed 's/^[[:space:]]*//'
}
write_lock_owner() {
  local lock_path="$1" current_start observed_pid observed_start
  current_start="$(process_start_token "$$")"
  [ -n "$current_start" ] || return 1
  printf '%s' "$$" >"$lock_path/owner.pid" \
    && printf '%s' "$current_start" >"$lock_path/owner.start" || return 1
  observed_pid="$(cat "$lock_path/owner.pid" 2>/dev/null || true)"; observed_start="$(cat "$lock_path/owner.start" 2>/dev/null || true)"
  [ "$observed_pid" = "$$" ] && [ -n "$observed_start" ] && [ "$observed_start" = "$current_start" ]
}
lock_owner_alive() {
  local lock_path="$1" owner_pid expected_start actual_start
  owner_pid="$(cat "$lock_path/owner.pid" 2>/dev/null || true)"
  expected_start="$(cat "$lock_path/owner.start" 2>/dev/null || true)"
  case "$owner_pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ -n "$expected_start" ] || return 1
  kill -0 "$owner_pid" 2>/dev/null || return 1
  actual_start="$(process_start_token "$owner_pid")"
  [ -n "$actual_start" ] && [ "$actual_start" = "$expected_start" ]
}
lock_identity() {
  stat -f '%d:%i:%m' "$1" 2>/dev/null || true
}
release_recovery_lock() {
  local current_owner=""
  if [ -f "$RECOVERY_LOCK_OWNER" ]; then
    current_owner="$(cat "$RECOVERY_LOCK_OWNER" 2>/dev/null || true)"
  fi
  if [ "$current_owner" = "$RECOVERY_LOCK_TOKEN" ]; then
    # Remove every metadata file before rmdir; leaving pid/start behind makes
    # every later wake see a false stale recovery owner.
    rm -f "$RECOVERY_LOCK_OWNER" \
      "$RECOVERY_LOCK_DIR/owner.pid" \
      "$RECOVERY_LOCK_DIR/owner.start" 2>/dev/null || true
    rmdir "$RECOVERY_LOCK_DIR" 2>/dev/null || true
  fi
}
release_publication_lock() {
  local owner_pid expected_start actual_start
  [ -d "$LOCK_DIR" ] || return 0
  owner_pid="$(cat "$LOCK_DIR/owner.pid" 2>/dev/null || true)"
  expected_start="$(cat "$LOCK_DIR/owner.start" 2>/dev/null || true)"
  actual_start="$(process_start_token "$$")"
  [ "$owner_pid" = "$$" ] || return 0
  [ -n "$expected_start" ] && [ "$expected_start" = "$actual_start" ] || return 0
  rm -f "$LOCK_DIR/owner.pid" "$LOCK_DIR/owner.start" 2>/dev/null || true
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
PUBLICATION_TRANSACTION_ACTIVE=0
NEW_LOCK_DIR=""
STALE_METADATA_DIR=""
DEFERRED_LOCK_SIGNAL=0
cleanup_stale_metadata_temp() { [ -n "${STALE_METADATA_DIR:-}" ] && [ -e "$STALE_METADATA_DIR" ] || return 0; for LOCK_METADATA in owner.token owner.pid owner.start; do rm -f "$STALE_METADATA_DIR/$LOCK_METADATA" 2>/dev/null || true; done; rmdir "$STALE_METADATA_DIR" 2>/dev/null || true; }
rollback_publication_transaction() {
  if [ -n "${STALE_QUARANTINE:-}" ] && [ -e "$STALE_QUARANTINE" ]; then
    if [ -n "${STALE_METADATA_DIR:-}" ] && [ -e "$STALE_METADATA_DIR" ]; then
      for LOCK_METADATA in owner.token owner.pid owner.start; do [ -e "$STALE_METADATA_DIR/$LOCK_METADATA" ] && mv "$STALE_METADATA_DIR/$LOCK_METADATA" "$STALE_QUARANTINE/$LOCK_METADATA" 2>/dev/null || true; done
      rmdir "$STALE_METADATA_DIR" 2>/dev/null || true
    fi
    if [ -n "${NEW_LOCK_DIR:-}" ] && [ -e "$NEW_LOCK_DIR" ]; then rm -f "$NEW_LOCK_DIR/owner.token" "$NEW_LOCK_DIR/owner.pid" "$NEW_LOCK_DIR/owner.start" 2>/dev/null || true; rmdir "$NEW_LOCK_DIR" 2>/dev/null || true; fi
    release_publication_lock
    [ ! -e "$LOCK_DIR" ] && mv "$STALE_QUARANTINE" "$LOCK_DIR" 2>/dev/null || true
  else
    if [ -n "${NEW_LOCK_DIR:-}" ] && [ -e "$NEW_LOCK_DIR" ]; then rm -f "$NEW_LOCK_DIR/owner.token" "$NEW_LOCK_DIR/owner.pid" "$NEW_LOCK_DIR/owner.start" 2>/dev/null || true; rmdir "$NEW_LOCK_DIR" 2>/dev/null || true; fi
    release_publication_lock
  fi
  PUBLICATION_TRANSACTION_ACTIVE=0
}
cleanup_article_locks() {
  [ "${PUBLICATION_TRANSACTION_ACTIVE:-0}" -ne 1 ] || rollback_publication_transaction
  cleanup_stale_metadata_temp
  release_publication_lock
  release_recovery_lock
}
if mkdir "$RECOVERY_LOCK_DIR" 2>/dev/null; then
  printf '%s' "$RECOVERY_LOCK_TOKEN" >"$RECOVERY_LOCK_OWNER"
  write_lock_owner "$RECOVERY_LOCK_DIR" || { release_recovery_lock; echo "=== $(date '+%F %T %Z') article-daily TERMINAL — recovery owner write/validation failed ===" >>"$LOG"; exit 1; }
else
  RECOVERY_SNAPSHOT="$(lock_identity "$RECOVERY_LOCK_DIR")"
  RECOVERY_MTIME="$(stat -f %m "$RECOVERY_LOCK_DIR" 2>/dev/null || echo 0)"
  RECOVERY_AGE=$(( $(date +%s) - ${RECOVERY_MTIME:-0} ))
  if [ -z "$RECOVERY_SNAPSHOT" ]; then
    echo "=== $(date '+%F %T %Z') article-daily SKIPPED — recovery lock identity unavailable ===" >>"$LOG"
    exit 0
  fi
  if lock_owner_alive "$RECOVERY_LOCK_DIR"; then
    echo "=== $(date '+%F %T %Z') article-daily SKIPPED — live recovery owner ===" >>"$LOG"
    exit 0
  fi
  if [ ! -s "$RECOVERY_LOCK_DIR/owner.pid" ] || [ ! -s "$RECOVERY_LOCK_DIR/owner.start" ]; then
    echo "=== $(date '+%F %T %Z') article-daily SKIPPED — recovery owner identity unavailable ===" >>"$LOG"
    exit 0
  fi
  RECOVERY_SNAPSHOT_NOW="$(lock_identity "$RECOVERY_LOCK_DIR")"
  if [ "$RECOVERY_SNAPSHOT" != "$RECOVERY_SNAPSHOT_NOW" ]; then
    echo "=== $(date '+%F %T %Z') article-daily SKIPPED — recovery lock identity changed ===" >>"$LOG"
    exit 0
  fi
  RECOVERY_QUARANTINE="$STATE_DIR/.article-daily.recovery.lockdir.stale.$$.$RANDOM"
  if ! mv "$RECOVERY_LOCK_DIR" "$RECOVERY_QUARANTINE" 2>/dev/null; then
    echo "=== $(date '+%F %T %Z') article-daily SKIPPED — recovery lock quarantine failed ===" >>"$LOG"
    exit 0
  fi
  if [ "$(lock_identity "$RECOVERY_QUARANTINE")" != "$RECOVERY_SNAPSHOT" ]; then
    [ ! -e "$RECOVERY_LOCK_DIR" ] && mv "$RECOVERY_QUARANTINE" "$RECOVERY_LOCK_DIR" 2>/dev/null || true
    echo "=== $(date '+%F %T %Z') article-daily SKIPPED — recovery quarantine identity changed ===" >>"$LOG"
    exit 0
  fi
  rm -f "$RECOVERY_QUARANTINE/owner.token" "$RECOVERY_QUARANTINE/owner.pid" "$RECOVERY_QUARANTINE/owner.start" 2>/dev/null || true
  if ! rmdir "$RECOVERY_QUARANTINE" 2>/dev/null || ! mkdir "$RECOVERY_LOCK_DIR" 2>/dev/null; then
    [ ! -e "$RECOVERY_LOCK_DIR" ] && mv "$RECOVERY_QUARANTINE" "$RECOVERY_LOCK_DIR" 2>/dev/null || true
    echo "=== $(date '+%F %T %Z') article-daily SKIPPED — recovery lock cleanup/reacquire failed ===" >>"$LOG"
    exit 0
  fi
  printf '%s' "$RECOVERY_LOCK_TOKEN" >"$RECOVERY_LOCK_OWNER"
  write_lock_owner "$RECOVERY_LOCK_DIR" || { release_recovery_lock; echo "=== $(date '+%F %T %Z') article-daily TERMINAL — recovery owner write/validation failed ===" >>"$LOG"; exit 1; }
fi
trap 'cleanup_article_locks' EXIT
if [ ! -e "$LOCK_DIR" ]; then
  PUBLICATION_TRANSACTION_ACTIVE=1
  DEFERRED_LOCK_SIGNAL=0; trap 'DEFERRED_LOCK_SIGNAL=143' TERM; trap 'DEFERRED_LOCK_SIGNAL=130' INT
  NEW_LOCK_DIR="$(mktemp -d "$STATE_DIR/.article-daily.lockdir.new.XXXXXX")"; DEFERRED_LOCK_RC=$?; trap - TERM INT
  if [ "$DEFERRED_LOCK_SIGNAL" -ne 0 ]; then rollback_publication_transaction; release_recovery_lock; exit "$DEFERRED_LOCK_SIGNAL"; fi
  if [ "$DEFERRED_LOCK_RC" -ne 0 ] || [ -z "$NEW_LOCK_DIR" ]; then rollback_publication_transaction; release_recovery_lock; echo "=== $(date '+%F %T %Z') article-daily TERMINAL — publication staging allocation failed ===" >>"$LOG"; exit 1; fi
  if ! write_lock_owner "$NEW_LOCK_DIR" || ! mv "$NEW_LOCK_DIR" "$LOCK_DIR" 2>/dev/null; then
    rollback_publication_transaction; release_recovery_lock; echo "=== $(date '+%F %T %Z') article-daily TERMINAL — publication owner staging/reacquire failed ===" >>"$LOG"; exit 1
  fi
  PUBLICATION_TRANSACTION_ACTIVE=0
else
  # stale-lock guard: a valid live owner always wins; a dead owner is quarantined
  # after start-token and directory-identity checks, regardless of lock age.
  if lock_owner_alive "$LOCK_DIR"; then
    release_recovery_lock
    echo "=== $(date '+%F %T %Z') article-daily SKIPPED — live publication owner ===" >>"$LOG"
    exit 0
  fi
  LOCK_OWNER_PID="$(cat "$LOCK_DIR/owner.pid" 2>/dev/null || true)"
  case "$LOCK_OWNER_PID" in
    ''|*[!0-9]*|0)
      release_recovery_lock
      echo "=== $(date '+%F %T %Z') article-daily TERMINAL — invalid or missing publication owner PID ===" >>"$LOG"
      exit 1
      ;;
  esac
  LEGACY_PID_ONLY=0; if [ ! -s "$LOCK_DIR/owner.start" ]; then
    LEGACY_PID_ONLY=1
    if kill -0 "$LOCK_OWNER_PID" 2>/dev/null; then
      release_recovery_lock
      echo "=== $(date '+%F %T %Z') article-daily TERMINAL — live PID-only publication owner is ambiguous ===" >>"$LOG"
      exit 1
    fi
  fi
    LOCK_SNAPSHOT="$(lock_identity "$LOCK_DIR")"
    LOCK_SNAPSHOT_NOW="$(lock_identity "$LOCK_DIR")"
    if [ -z "$LOCK_SNAPSHOT" ] || [ "$LOCK_SNAPSHOT" != "$LOCK_SNAPSHOT_NOW" ]; then
      release_recovery_lock
      echo "=== $(date '+%F %T %Z') article-daily TERMINAL — stale publication lock identity changed ===" >>"$LOG"
      exit 1
    fi
    STALE_QUARANTINE="$STATE_DIR/.article-daily.lockdir.stale.$$.$RANDOM"
    NEW_LOCK_DIR=""
    STALE_METADATA_DIR=""
    fail_stale_publication_recovery() { release_recovery_lock; echo "=== $(date '+%F %T %Z') article-daily TERMINAL — stale publication lock recovery failed for $LOCK_DIR ===" >>"$LOG"; exit 1; }
    fail_stale_publication_quarantine() { release_recovery_lock; echo "=== $(date '+%F %T %Z') article-daily TERMINAL — stale publication lock quarantine failed ===" >>"$LOG"; exit 1; }
    [ ! -e "$STALE_QUARANTINE" ] || { release_recovery_lock; echo "=== $(date '+%F %T %Z') article-daily TERMINAL — stale publication lock quarantine failed ===" >>"$LOG"; exit 1; }
    PUBLICATION_TRANSACTION_ACTIVE=1
    [ "$LEGACY_PID_ONLY" -eq 1 ] && kill -0 "$LOCK_OWNER_PID" 2>/dev/null && { PUBLICATION_TRANSACTION_ACTIVE=0; release_recovery_lock; echo "=== $(date '+%F %T %Z') article-daily TERMINAL — live PID-only publication owner is ambiguous ===" >>"$LOG"; exit 1; }
    if ! mv "$LOCK_DIR" "$STALE_QUARANTINE" 2>/dev/null; then rollback_publication_transaction; fail_stale_publication_quarantine; fi
    if [ "$(lock_identity "$STALE_QUARANTINE")" != "$LOCK_SNAPSHOT" ]; then rollback_publication_transaction; fail_stale_publication_recovery; fi
    DEFERRED_LOCK_SIGNAL=0; trap 'DEFERRED_LOCK_SIGNAL=143' TERM; trap 'DEFERRED_LOCK_SIGNAL=130' INT
    NEW_LOCK_DIR="$(mktemp -d "$STATE_DIR/.article-daily.lockdir.new.XXXXXX")"; DEFERRED_LOCK_RC=$?; trap - TERM INT
    if [ "$DEFERRED_LOCK_SIGNAL" -ne 0 ]; then rollback_publication_transaction; release_recovery_lock; exit "$DEFERRED_LOCK_SIGNAL"; fi
    if [ "$DEFERRED_LOCK_RC" -ne 0 ] || [ -z "$NEW_LOCK_DIR" ]; then rollback_publication_transaction; fail_stale_publication_recovery; fi
    if ! write_lock_owner "$NEW_LOCK_DIR"; then rollback_publication_transaction; fail_stale_publication_recovery; fi
    if ! mv "$NEW_LOCK_DIR" "$LOCK_DIR" 2>/dev/null; then rollback_publication_transaction; fail_stale_publication_recovery; fi
    DEFERRED_LOCK_SIGNAL=0; trap 'DEFERRED_LOCK_SIGNAL=143' TERM; trap 'DEFERRED_LOCK_SIGNAL=130' INT
    STALE_METADATA_DIR="$(mktemp -d "$STATE_DIR/.article-daily.lockdir.metadata.XXXXXX")"; DEFERRED_LOCK_RC=$?; trap - TERM INT
    if [ "$DEFERRED_LOCK_SIGNAL" -ne 0 ]; then rollback_publication_transaction; release_recovery_lock; exit "$DEFERRED_LOCK_SIGNAL"; fi
    if [ "$DEFERRED_LOCK_RC" -ne 0 ] || [ -z "$STALE_METADATA_DIR" ]; then rollback_publication_transaction; fail_stale_publication_recovery; fi
    for LOCK_METADATA in owner.token owner.pid owner.start; do
      if [ -e "$STALE_QUARANTINE/$LOCK_METADATA" ] && ! mv "$STALE_QUARANTINE/$LOCK_METADATA" "$STALE_METADATA_DIR/$LOCK_METADATA" 2>/dev/null; then rollback_publication_transaction; fail_stale_publication_recovery; fi
    done
    if ! rmdir "$STALE_QUARANTINE" 2>/dev/null; then rollback_publication_transaction; fail_stale_publication_recovery; fi
    PUBLICATION_TRANSACTION_ACTIVE=0
    cleanup_stale_metadata_temp
fi
release_recovery_lock

# START CONTROL: durable active-four/resume state prevents replaying one unfinished run, while
# a completed run releases the scheduler to allocate a fresh immutable run for another article.
# This is deliberately not a one-article-per-JST-day quota.
START_CONTROL="$ARTICLE_ROOT/scripts/article_daily_start_control.py"
TODAY_JST="$(TZ=Asia/Tokyo date +%F)"
ALLOCATED_NEW_RUN_ID=""
reserve_new_run_id() {
  local run_id="$1"
  if mkdir "$STATE_DIR/runs/$run_id" 2>/dev/null; then
    ALLOCATED_NEW_RUN_ID="$run_id"
    return 0
  fi
  return 1
}
allocate_new_run_id() {
  local candidate
  mkdir -p "$STATE_DIR/runs"
  while :; do
    candidate="$(date -u '+%Y%m%d-%H%M%S')"
    if reserve_new_run_id "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
    # The state directory is shared by launchd workers. A collision is rare,
    # but never reuse an existing run path or let mkdir -p merge two runs.
    sleep 1
  done
}
START_DECISION="$(python3 "$START_CONTROL" --state-dir "$STATE_DIR" --local-date "$TODAY_JST" 2>>"$LOG" || printf '%s' '{"action":"block-incomplete","reason":"start-control-error"}')"
START_ACTION="$(printf '%s' "$START_DECISION" | jq -r '.action // "block-incomplete"')"
START_RUN_ID="$(printf '%s' "$START_DECISION" | jq -r '.run_id // empty')"
START_REASON="$(printf '%s' "$START_DECISION" | jq -r '.reason // empty')"
if [ -n "${ARTICLE_EXPECTED_NEW_DAILY_DATE:-}" ] \
  && { [ "$TODAY_JST" != "$ARTICLE_EXPECTED_NEW_DAILY_DATE" ] \
    || [ "$START_ACTION" != "new" ] \
    || [ "$START_REASON" != "no-same-jst-day-run" ]; }; then
  echo "=== article-daily missed-schedule catch-up no longer owns a new run expected_date=$ARTICLE_EXPECTED_NEW_DAILY_DATE decision=$START_DECISION; no generation side effect ===" >>"$LOG"
  exit 0
fi
if [ -n "${ARTICLE_EXPECTED_NEW_RUN_ID:-}" ] \
  && { [ "$START_ACTION" != "new-quality-replacement" ] \
    || [ "$START_RUN_ID" != "$ARTICLE_EXPECTED_NEW_RUN_ID" ]; }; then
  echo "=== article-daily expected quality replacement mismatch expected=$ARTICLE_EXPECTED_NEW_RUN_ID decision=$START_DECISION; no generation side effect ===" >>"$LOG"
  exit 0
fi
if [ -n "${ARTICLE_EXPECTED_RUN_ID:-}" ] \
  && { [ "$START_ACTION" != "resume-generation" ] \
    || [ "$START_RUN_ID" != "$ARTICLE_EXPECTED_RUN_ID" ]; }; then
  echo "=== article-daily expected recovery run mismatch expected=$ARTICLE_EXPECTED_RUN_ID decision=$START_DECISION; no generation side effect ===" >>"$LOG"
  exit 0
fi
if [ "$START_ACTION" = "new" ] && [ -z "$START_RUN_ID" ]; then
  START_RUN_ID="$(allocate_new_run_id)"
  ALLOCATED_NEW_RUN_ID="$START_RUN_ID"
  START_DECISION="$(printf '%s' "$START_DECISION" | jq --arg run_id "$START_RUN_ID" '. + {run_id:$run_id}')"
  echo "=== article-daily start control: completed prior run released a new run=$START_RUN_ID reason=$(printf '%s' "$START_DECISION" | jq -r '.reason // "new-article"') $(date '+%F %T %Z') ===" >>"$LOG"
fi
RESUME_GENERATION=0
if [ "$ARTICLE_PUBLICATION_POLICY" = "continuous" ] \
  && [ "$START_ACTION" = "skip-quality-miss" ]; then
  PREVIOUS_QUALITY_RUN="$START_RUN_ID"
  START_ACTION="new"
  START_RUN_ID="$(allocate_new_run_id)"
  ALLOCATED_NEW_RUN_ID="$START_RUN_ID"
  START_DECISION="$(jq -cn --arg run_id "$START_RUN_ID" --arg replaced "$PREVIOUS_QUALITY_RUN" \
    '{action:"new",run_id:$run_id,replaced_run_id:$replaced,reason:"continuous-publication-after-quality-advisory"}')"
  echo "=== article-daily continuous policy: quality replacement limit is advisory; starting run=$START_RUN_ID after=$PREVIOUS_QUALITY_RUN $(date '+%F %T %Z') ===" >>"$LOG"
fi
case "$START_ACTION" in
  skip-complete)
    # Compatibility with an older start-control helper: a completed run is no longer a
    # same-day stop. Allocate a new identity and continue through the normal topic selector.
    PREVIOUS_COMPLETE_RUN="$START_RUN_ID"
    START_ACTION="new"
    START_RUN_ID="$(allocate_new_run_id)"
    ALLOCATED_NEW_RUN_ID="$START_RUN_ID"
    START_DECISION="$(jq -cn --arg run_id "$START_RUN_ID" --arg previous "$PREVIOUS_COMPLETE_RUN" \
      '{action:"new",run_id:$run_id,previous_run_id:$previous,reason:"completed-run-new-article-allowed"}')"
    echo "=== article-daily start control: legacy completed-run stop lifted previous=$PREVIOUS_COMPLETE_RUN new=$START_RUN_ID $(date '+%F %T %Z') ===" >>"$LOG"
    ;;
  skip-pending-worker)
    echo "=== article-daily start control: pending active-four run remains resume-worker-owned run=$START_RUN_ID; no concurrent article $(date '+%F %T %Z') ===" >>"$LOG"
    exit 0
    ;;
  resume-generation)
    RESUME_GENERATION=1
    START_REASON="$(printf '%s' "$START_DECISION" | jq -r '.reason // "unknown"')"
    echo "=== article-daily start control: safely resuming pre-publication generation run=$START_RUN_ID reason=$START_REASON with immutable prompt $(date '+%F %T %Z') ===" >>"$LOG"
    ;;
  new)
    ;;
  new-quality-replacement)
    echo "=== article-daily quality replacement: new run=$START_RUN_ID replaces=$(printf '%s' "$START_DECISION" | jq -r '.replaced_run_id') $(date '+%F %T %Z') ===" >>"$LOG"
    ;;
  *)
    echo "=== article-daily start control BLOCK: $START_DECISION; no unsafe duplicate or ambiguous run $(date '+%F %T %Z') ===" >>"$LOG"
    if command -v telegram_notify >/dev/null 2>&1; then
      telegram_notify "article-daily stopped because the saved run is ambiguous or not safely resumable: $START_DECISION" >>"$LOG" 2>&1 || true
    fi
    exit 0
    ;;
esac

# Quality replacement IDs are selected by the start controller from a trusted
# terminal timestamp. Reserve that exact path atomically as well; if another
# owner already claimed it, use the same collision-safe allocator rather than
# merging into an existing run directory.
if [ "$START_ACTION" = "new-quality-replacement" ] \
  && [ "$ALLOCATED_NEW_RUN_ID" != "$START_RUN_ID" ]; then
  if ! reserve_new_run_id "$START_RUN_ID"; then
    PREVIOUS_QUALITY_RUN="$START_RUN_ID"
    START_RUN_ID="$(allocate_new_run_id)"
    ALLOCATED_NEW_RUN_ID="$START_RUN_ID"
    START_DECISION="$(printf '%s' "$START_DECISION" | jq --arg run_id "$START_RUN_ID" '. + {run_id:$run_id}')"
    echo "=== article-daily quality replacement ID collision; reserved new=$START_RUN_ID previous_candidate=$PREVIOUS_QUALITY_RUN $(date '+%F %T %Z') ===" >>"$LOG"
  fi
fi

# RUN RECORD (spec docs/loop-engineering/47-writer-loop-quality-and-self-improvement.md, meta-harness
# ablation: the single most important thing to keep is the raw trace of every pass, not a summary of
# it). One dir per pass under state/runs/<UTC timestamp>/; the wrapper here writes what it can prove
# mechanically (git hash of the harness at pass-start, this pass raw model stdout after it finishes);
# the PROMPT below asks the agent to also copy its own draft + gate JSONs + an optional why.md into the
# SAME dir so one folder holds everything needed to reconstruct what this pass actually did.
RUNS_ROOT="$STATE_DIR/runs"
RUN_TS="${START_RUN_ID:-daily-$TODAY_JST}"
RUN_DIR="$RUNS_ROOT/$RUN_TS"
if [ "$ALLOCATED_NEW_RUN_ID" = "$RUN_TS" ]; then
  mkdir "$RUN_DIR/gates" 2>/dev/null || {
    echo "=== article-daily reserved run could not create gates run=$RUN_TS; refusing merge ===" >>"$LOG"
    exit 1
  }
else
  # Existing directories are permitted only for an explicitly resumable run
  # selected by start control. A new run never uses mkdir -p on an existing path.
  [ -d "$RUN_DIR" ] || {
    echo "=== article-daily expected existing run is missing run=$RUN_TS; refusing merge ===" >>"$LOG"
    exit 1
  }
  if [ ! -d "$RUN_DIR/gates" ]; then
    mkdir "$RUN_DIR/gates" 2>/dev/null || {
      echo "=== article-daily existing run gates creation failed run=$RUN_TS ===" >>"$LOG"
      exit 1
    }
  fi
fi
if [ "$START_ACTION" = "new-quality-replacement" ]; then
  QUALITY_REPLACEMENT_TMP="$RUN_DIR/gates/.quality-replacement.json.$$"
  printf '%s' "$START_DECISION" | jq -c '{
    version: 2,
    replacement_run_id: .run_id,
    replaced_run_id,
    forbidden_topic_id,
    forbidden_editorial_form,
    quality_failure_feedback,
    reason
  }' >"$QUALITY_REPLACEMENT_TMP" \
    && mv "$QUALITY_REPLACEMENT_TMP" "$RUN_DIR/gates/quality-replacement.json" || {
      rm -f "$QUALITY_REPLACEMENT_TMP"
      echo "=== article-daily quality replacement receipt failed closed ===" >>"$LOG"
      exit 1
    }
fi
if [ "$RESUME_GENERATION" -ne 1 ]; then
  (cd "$ARTICLE_ROOT" && git log -1 --format='harness_git_hash=%H' -- SKILL.md scripts vendor 2>/dev/null || echo "harness_git_hash=UNKNOWN") > "$RUN_DIR/git-hash.txt"
fi
python3 "$ARTICLE_ROOT/scripts/strategy_runtime.py" \
  consume-active --run-id "$RUN_TS" \
  --active-dir "$STATE_DIR/strategy/active" \
  --out "$RUN_DIR/gates/strategy-consumption.json" >>"$LOG" 2>&1 || {
  echo "=== article-daily active strategy consumption blocked generation ===" >>"$LOG"
  telegram_notify "Writer blocked: active strategy hash drift. receipt=$RUN_DIR/gates/strategy-consumption.json" || true
  exit 1
}
# self-heal L2 (spec #22): recover the shared daily-driver browser BEFORE the pass needs it.
# ensure_browser.sh already does exactly this (relaunch Chromium if :9222 is dead, restore the
# session vault, GC stale CDP contexts) but this loop never called it -- confirmed by grep,
# zero hits, before this line existed. Runs exactly once, inside the lock this script already
# holds, so it can never race the 5-min ai.anicca.article-healthcheck poller, which
# deliberately does not touch the browser at all for this same reason.
BROWSER_GUARD="${LIFE_MANAGER_REPO:-$(cd "$ARTICLE_ROOT/../.." && pwd)}/skills/browser/ensure_browser.sh"
if [ -x "$BROWSER_GUARD" ]; then
  BROWSER_STATUS="$(
    CLOAK_CDP_BASE_URL="${WRITER_CDP_URL:-http://127.0.0.1:9222}" \
    CDP_DAILY_DRIVER_PORT="${WRITER_CDP_PORT:-9222}" \
    CDP_DAILY_DRIVER_PROFILE="${WRITER_CDP_PROFILE:-$HOME/.cloak/profiles/job-search-daily}" \
    CLOAK_BROWSER_LAUNCHD_LABEL="${WRITER_BROWSER_LAUNCHD_LABEL:-ai.anicca.job-search-browser}" \
    bash "$BROWSER_GUARD" 2>>"$LOG" | tail -1
  )"
else
  BROWSER_STATUS="FAILED: browser guard missing at $BROWSER_GUARD"
fi
echo "=== article-daily ensure_browser: ${BROWSER_STATUS:-EMPTY} ===" >>"$LOG"

# PRE-PUBLICATION RESUME CARD RECOVERY: the first pass legitimately claims its demand card
# before research.  If that pass stops before publication, the card remains in in-progress while
# the same immutable run is resumed.  Restore only the exact card bound to this run before the
# prompt's normal demand-authority + selector steps; never turn an unrelated in-progress card into
# a fresh topic and never recover after publication state exists.  The receipt is durable evidence
# of the state transition, not an agent assertion.
if [ "$RESUME_GENERATION" -eq 1 ]; then
  python3 - "$RUN_DIR" "$STATE_DIR" "$RUN_TS" >>"$LOG" <<'PYEOF'
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

run_dir, state_dir, run_id = map(Path, sys.argv[1:])
run_id = str(run_id)
gates = run_dir / "gates"
route_path = gates / "topic-route-input.json"
publication_state = gates / "publication-state.json"
receipt_path = gates / "topic-card-resume.json"
queue = state_dir / "topics" / "queue"
in_progress = state_dir / "topics" / "in-progress"
topic_route_path = gates / "topic-route.json"
ledger = state_dir / "articles.jsonl"

def write_receipt(payload: dict) -> None:
    if receipt_path.is_symlink() or (receipt_path.exists() and not receipt_path.is_file()):
        raise SystemExit("topic-card resume blocked: receipt path is not a regular file")
    temporary = receipt_path.with_name(f".{receipt_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, receipt_path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

def fail(reason: str) -> None:
    write_receipt({"version": 1, "run_id": run_id, "action": "blocked", "reason": reason})
    raise SystemExit(f"topic-card resume blocked: {reason}")

def path_status(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "regular"
    return "nonregular"

if publication_state.exists() or publication_state.is_symlink():
    fail("publication-state-exists")
route_status = path_status(route_path)
if route_status == "symlink":
    fail("topic-route-input-symlink")
if route_status == "unreadable":
    fail("topic-route-input-unreadable")
if route_status == "nonregular":
    fail("topic-route-input-nonregular")
if route_status == "absent":
    # A SIGTERM before topic selection has no card to restore.  Resume the same
    # immutable prompt only when the generation journal proves that exact empty
    # boundary; every route-bearing or ambiguous run remains fail-closed.
    generation_path = gates / "generation-state.json"
    if path_status(generation_path) != "regular":
        fail("generation-state-missing-or-symlink")
    if path_status(ledger) != "regular":
        fail("ledger-missing-or-symlink")
    try:
        generation = json.loads(generation_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        fail("generation-state-invalid")
    if not isinstance(generation, dict):
        fail("generation-state-invalid")
    attempts = generation.get("attempts")
    latest = attempts[-1] if isinstance(attempts, list) and attempts else None
    archive_manifest = latest.get("archive_manifest") if isinstance(latest, dict) else None
    empty_interruption = (
        generation.get("version") == 1
        and generation.get("run_id") == run_id
        and generation.get("status") == "interrupted-safe"
        and isinstance(latest, dict)
        and latest.get("status") == "interrupted-safe"
        and latest.get("boundary") == "archived-prepublication-artifacts"
        and isinstance(archive_manifest, list)
        and all(
            isinstance(item, dict)
            and item.get("path") in {
                "gates/selfimprove-verify.json",
                "gates/required-demand-authority-blocker.json",
                "gates/telegram-report.json",
            }
            for item in archive_manifest
        )
        and path_status(topic_route_path) == "absent"
    )
    empty_provider_failure = (
        generation.get("version") == 1
        and generation.get("run_id") == run_id
        and generation.get("status") == "provider-failed-ambiguous"
        and isinstance(latest, dict)
        and isinstance(latest.get("return_code"), int)
        and latest["return_code"] != 0
        and latest.get("boundary") == "prepublication-empty"
        and path_status(topic_route_path) == "absent"
    )
    public_row = False
    try:
        ledger_lines = ledger.read_text(encoding="utf-8").splitlines()
    except OSError:
        fail("ledger-invalid")
    for line in ledger_lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            fail("ledger-invalid")
        if not isinstance(row, dict):
            fail("ledger-invalid")
        if (
            row.get("run_id") == run_id
            and (
                row.get("published") is True
                or bool(row.get("live_url"))
                or row.get("state") == "live"
                or row.get("reality_gate") == "PASS"
            )
        ):
            public_row = True
    if (empty_interruption or empty_provider_failure) and not public_row:
        write_receipt({
            "version": 1,
            "run_id": run_id,
            "action": "skip-pre-topic-recovery",
            "reason": "empty-pre-topic-interruption",
        })
        print("topic-card resume: skipped pre-topic recovery")
        raise SystemExit(0)
    fail("topic-route-input-missing")
try:
    route = json.loads(route_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    fail("topic-route-input-invalid")
topic_id = route.get("topic_id") if isinstance(route, dict) else None
if not isinstance(topic_id, str) or not topic_id.startswith("paid-demand:"):
    fail("topic-id-not-paid-demand")
if queue.is_symlink() or not queue.is_dir() or in_progress.is_symlink() or not in_progress.is_dir():
    fail("topic-stage-missing-or-symlink")

def card_topic_id(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("topic_id:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return None

matching_queue = [p for p in sorted(queue.glob("*.md")) if p.is_file() and not p.is_symlink() and card_topic_id(p) == topic_id]
matching_progress = [p for p in sorted(in_progress.glob("*.md")) if p.is_file() and not p.is_symlink() and card_topic_id(p) == topic_id]
if len(matching_queue) + len(matching_progress) > 1:
    fail("duplicate-topic-card")
if matching_queue:
    source = matching_queue[0]
    action = "already-queued"
elif matching_progress:
    source = matching_progress[0]
    destination = queue / source.name
    if destination.exists() or destination.is_symlink():
        fail("destination-name-conflict")
    os.replace(source, destination)
    source = destination
    action = "restored-in-progress-card"
else:
    fail("matching-card-not-found")

digest = hashlib.sha256(source.read_bytes()).hexdigest()
write_receipt({
        "version": 1,
        "run_id": run_id,
        "action": action,
        "topic_id": topic_id,
        "basename": source.name,
        "sha256": digest,
        "queue_path": str(source),
    })
print(f"topic-card resume: {action} basename={source.name} topic_id={topic_id} sha256={digest}")
PYEOF
  if [ "$?" -ne 0 ]; then
    echo "=== article-daily topic-card resume failed closed ===" >>"$LOG"
    exit 75
  fi
  RESUME_CARD_ACTION="$(jq -r '.action // empty' "$RUN_DIR/gates/topic-card-resume.json" 2>/dev/null || true)"
  if [ "$RESUME_CARD_ACTION" = "skip-pre-topic-recovery" ]; then
    unset ARTICLE_RESUME_CARD_BASENAME
    echo "=== article-daily topic-card resume skipped: empty pre-topic interruption ===" >>"$LOG"
  else
    RESUME_CARD_BASENAME="$(jq -r '.basename // empty' "$RUN_DIR/gates/topic-card-resume.json" 2>/dev/null || true)"
    if [ -z "$RESUME_CARD_BASENAME" ] || [[ "$RESUME_CARD_BASENAME" == */* ]]; then
      echo "=== article-daily topic-card resume selector binding failed closed ===" >>"$LOG"
      exit 75
    fi
    export ARTICLE_RESUME_CARD_BASENAME="$RESUME_CARD_BASENAME"
  fi
fi

# DEMAND AUTHORITY PREFLIGHT: this is a wrapper-side gate, before PROMPT construction,
# generation state, the judge broker, or any provider invocation. A missing/invalid
# claim-loop supply is a durable pending run, never a reason to spend a model call.
DEMAND_AUTHORITY_SCRIPT="$ARTICLE_ROOT/scripts/demand_authority.py"
if ! python3 "$DEMAND_AUTHORITY_SCRIPT" \
  --skill-dir "$ARTICLE_ROOT" \
  --demand-mode required >>"$LOG" 2>&1; then
  echo "=== article-daily demand authority blocked generation; pending claim-loop supply ===" >>"$LOG"
  if command -v telegram_notify >/dev/null 2>&1; then
    telegram_notify "Writer pending: required paid-demand claim-loop supply is not ready; no provider invocation occurred." >>"$LOG" 2>&1 || true
  fi
  exit 75
fi

PROMPT='Run ONE daily Writer Agent article pass, no daily human in the loop. This pass was triggered by a real launchd daily schedule (ai.anicca.article-daily) -- you do NOT need to register your own recurring scheduler; launchd is the only scheduler for this loop, never self-register one via any cron-creation tool. The wrapper has already loaded the Life Manager environment; never source another runtime environment.

CURRENT BRAKE SNAPSHOT: the wrapper checked ARTICLE_PUBLICATION_PAUSE_FILE immediately before building this prompt and found PUBLICATION_PAUSE_SNAPSHOT_PLACEHOLDER. Treat only that current filesystem check as pause evidence. A historical "publication paused" line in article-daily.log, an older run directory, or a stale manifest is not current state; when the snapshot is absent, continue through the normal gates and publisher-native readbacks.

★ DO THE WORK YOURSELF, IN THIS PASS, IN THE FOREGROUND. ★ Do not hand the job to a background agent and finish. The first real run did exactly that: it spawned a background worker, reported that it was waiting for it, and exited with a green rc=0 having produced zero articles. You are the worker. Research, write, stage, verify and report inside this pass, and only return when the drafts genuinely exist and you have looked at them. A clean exit code with nothing to show for it is the precise failure this loop exists to prevent.

★ SOURCE IMMUTABILITY — HARD BOUNDARY. ★ Never edit tracked source files during a daily run, including SKILL.md, scripts, tests, references, configuration, or launchd files. Runtime writes are limited to the Article Writer state tree plus the existing platform/browser workspaces exposed by the model runner. If a script or gate has a code defect, record the exact defect under the current run gates, leave the affected pair pending, continue independent pairs, and report it. Do not spawn a fixer, change permissions, bypass the sandbox, commit, or push source.

★ DORMANT DESTINATIONS — HARD BOUNDARY. ★ Zenn JA, Dev.to EN, X Article EN, and X Post JA are dormant under the current active-four contract. Never stage them, create an intent, invoke an adapter or publisher, hand them to a retry worker, or repair them. Armed runs persist only their explicit durable skip receipts; unarmed runs do not create destination state for them.

★ ZENN FRONT MATTER — LEGACY-EXACT8 COMPATIBILITY ONLY. ★ The legacy-exact8 adapter remains the only sanctioned path for its historical Zenn artifact: use scripts/zenn-publish/zenn-adapt.py and never hand-write the zenn front matter. The current active-four path never invokes that adapter or stages that destination.

★ DESTINATIONS ARE INDEPENDENT — HARD BOUNDARY. ★ A destination that cannot be staged after bounded retries (dead API credential, platform 4xx/5xx, editor unreachable) must never stop the others: run `python3 ARTICLE_ROOT_PLACEHOLDER/scripts/publication-guard.py mark-unavailable --pair <pair> --reason "<machine-readable reason>"` for only that pair, then continue every stageable destination; never abandon the remaining destinations. Editorial and reader findings are advisory under ARTICLE_PUBLICATION_POLICY=continuous: record them for the next learning cycle, but do not stop this run. Identity/safety, secret/PII, duplicate, payload-integrity, and platform-policy failures remain blocking.

★ JUDGE BROKER — HARD BOUNDARY. ★ Every judge/vision model call is served by the wrapper-side judge broker through the model runner. When invoking any gate or the model runner, never clear, unset, or override ARTICLE_NESTED_SANDBOX, ARTICLE_RUN_DIR, or ARTICLE_JUDGE_BROKER_SERVER, and never bypass the judge broker by spawning a provider CLI directly: a direct provider spawn inside the bounded sandbox always fails, poisons provider health for the whole run, and forces every later safety gate to fail closed. If a judge call returns no verdict, record the failure and leave the pair pending; do not retry with altered environment variables.

STEP 0 (SELF-FIX RESULT CHECK -- existence-guarded, run before STEP 1): read ARTICLE_STATE_DIR_PLACEHOLDER/.self-fix-writer-agent.result if it exists (it will not exist until STEP 6.5 has spawned a fixer at least once -- if absent, skip this step silently and go to STEP 1). If its first word is FAIL, a previous pass hit a render-verify problem its self-fix attempt could not resolve -- read the rest of that line plus the tail of WRITER_LOG_DIR_PLACEHOLDER/self-fix-writer-agent.log for the real diagnosis, and stay extra alert to that same class of defect (which platform, which rule) while writing and staging todays article; this is context only, never a reason to skip or delay todays pass. If its first word is SUCCESS, a prior self-fix genuinely resolved something -- no action needed. If its first word is RUNNING, a fixer may still be active or may have crashed stale (article-self-fix.sh has its own staleness/respawn logic for that, nothing for you to do here).

STEP 0.5 (SELF-IMPROVE TODO CHECK -- mandatory, spec docs/loop-engineering/47-writer-loop-quality-and-self-improvement.md §7 principle 7, run before STEP 1): run bash ARTICLE_ROOT_PLACEHOLDER/scripts/article-selfimprove-verify.sh. It inspects the most recently completed ARTICLE_STATE_DIR_PLACEHOLDER/runs/ generation against REAL file evidence (gate JSON content, articles.jsonl rows) -- never self-report, never mtime alone -- and writes ARTICLE_STATE_DIR_PLACEHOLDER/.selfimprove-todo.json. If its missing array is non-empty, treat every item as this passs first priority: e.g. if a prior run claims all gates passed but has no matching articles.jsonl row with a real staged editor URL, or a gate JSON that STEP 4/4.6/4.7 mandates is missing from a prior runs/ dir, that is a real gap in what got proven, not just reported -- make sure THIS pass writes every gate JSON into its own run dir (STEP 0.6) and its ledger row (STEP 7) without fail, so the same gap is not repeated. Do not skip this step because you believe everything is fine; self-report is not evidence, only real files are.

STEP 0.6 (RUN RECORD -- persist this passs raw trace, spec docs/loop-engineering/47-writer-loop-quality-and-self-improvement.md, meta-harness ablation): this pass has a fixed record directory already created for you at ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER/ (git-hash.txt is already written there). As you go: after STEP 3 (write), copy the final ja/en markdown drafts there as article-ja.md and article-en.md; after STEP 4/STEP 4.6 gates, copy each gate JSON verdict there as gates/<gate-name>-<lang>.json (deslop-gate, eval-gate, identity-gate, rubric-judge -- whichever ran, every attempt including revise loops). Never delete or move this directory yourself -- retention/pruning is handled by the wrapper script, not by you.

STEP 0.7 (MID TOKEN/ATTEMPT BUDGET -- bounded, whole bilingual article): record PASS_STARTED_AT=$(date +%s), QUALITY_REVISE_ATTEMPTS=0, READER_ATTEMPTS=0, plus per-language counters in this runs attempt-budget state. The MID budget is at most $4/day inside the all-lane ceiling of $6/day. scripts/attempt-budget-gate.sh permits at most 6 total editorial and 6 total reader evaluations across both languages. Before every additional evaluation, call the budget gate. STOP_SPENDING preserves the best draft and raw evidence; it never fabricates PASS. Editorial or reader terminal failure proceeds to STEP 4.75 quality self-heal, not publication.

STEP 0.8 (READ VERIFIED ACTIVE STRATEGY AND REPLAY-FIRST CANARY BEFORE TOPIC SELECTION): first read this runs gates/strategy-consumption.json, which the wrapper created from hash-verified active strategy bytes before invoking you. If its versions contains slice=writer-learning, require exactly one such row, read only its immutable weight_file, and remember learning_cycle_id plus consumed_hash. Read the matching ARTICLE_STATE_DIR_PLACEHOLDER/learning/experiments/<learning_cycle_id>/manifest.json and require candidate_strategy_sha256=consumed_hash. Apply the complete immutable strategy object to BOTH native drafts: its playbook is the writing baseline and every other key is an active writing rule. It cannot override safety, citations, identity, active-four, price, platform, schedules, providers, destination accounts, or recovery. Do not call this consumed yet; STEP 4.85 binds the final article bytes and visible excerpts. Then run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/writer_learning_worker.py current --skill-dir ARTICLE_ROOT_PLACEHOLDER. If status is NONE, continue without inventing a candidate. If status is CANDIDATE_CANARY, remember its experiment_id, strategy_sha256, exact reader_job, immutable strategy_path, one changed_field, and one rule before entering STEP 1. During STEP 1, use the candidate only if an honest claim/topic can preserve that exact reader_job; otherwise leave the assignment READY, select a normal reader-useful topic, and publish without the candidate. A learning assignment never blocks todays publication.

★ SCOPE, READ FIRST: DEFAULT UNARMED MODE (ARTICLE_AUTOPUBLISH unset or not 1) stages DRAFTS ONLY and NEVER publishes. In that default mode, a human (Dais) reads every draft and publishes by hand; run.sh STEP 7 treats a publicly-live article as a SAFETY FAILURE; publish-to-x.sh go and every other live-publish endpoint are forbidden; every ledger publish flag stays false. ARMED MODE is different: only when the ARTICLE_AUTOPUBLISH=1 addendum is appended below, the same immutable package must reach active-four. The foreground publishes note/ja, substack/ja, substack/en, and x-article/ja in the specified order; all four dormant destinations receive explicit skip receipts and are never staged or published by this loop. Use only STEPS 11-20 and never infer permission outside that exact addendum and scope.

CANONICAL SKILL IDENTITY: writer-agent. The tracked ARTICLE_ROOT_PLACEHOLDER path is the current compatibility location for this one pipeline; never start or restore a second article-writing loop.

STEP 1 (TOPIC SELECTION): the required-demand authority and queue selection below own topic choice; a failed authority check is a hard stop before generation.

Before any topic selection, run `python3 ARTICLE_ROOT_PLACEHOLDER/scripts/demand_authority.py --skill-dir ARTICLE_ROOT_PLACEHOLDER --demand-mode required`; a non-zero exit is a hard STOP with no generation or publication. Run `bash ARTICLE_ROOT_PLACEHOLDER/scripts/select-next-topic.sh ARTICLE_STATE_DIR_PLACEHOLDER/topics/queue/`. This required-demand queue is the only topic authority. If it exits 0, move its one stdout card to `ARTICLE_STATE_DIR_PLACEHOLDER/topics/in-progress/` and use the card as source material. If it exits 1, STOP and leave the run pending until claim-loop demand supply is ready; never inspect `ARTICLE_STATE_DIR_PLACEHOLDER/topic-queue.md` or self-select a fallback topic. If `$ARTICLE_RUN_DIR/gates/quality-replacement.json` exists, this is the one bounded recovery candidate for the current JST-date quality slot: its forbidden_topic_id and forbidden_editorial_form MUST both change, and every quality_failure_feedback item MUST be covered by at least one `evidence_plan[].addresses_feedback` ID before research proceeds. `topic_router.py` enforces topic, form, feedback hash, and exact feedback coverage. Before research, write `$ARTICLE_RUN_DIR/gates/topic-route-input.json` with independent topic_source, reader {audience,job,outcome}, evidence_plan, editorial_form, and product_link. Choose editorial_form only from explainer, how-to, case-study, comparison, field-note, opinion, or report. Run `python3 ARTICLE_ROOT_PLACEHOLDER/scripts/topic_router.py validate --input "$ARTICLE_RUN_DIR/gates/topic-route-input.json" --demand-mode required --out "$ARTICLE_RUN_DIR/gates/topic-route.json" --runs-root ARTICLE_STATE_DIR_PLACEHOLDER/runs --current-run-id RUN_DIR_PLACEHOLDER`. If the router rejects a third consecutive form or the blocked replacement route, choose a different topic/form that still fits the reader job; never change evidence or fabricate experience merely to pass. The frozen route controls writing: explainer/how-to/comparison/report are reader-centred and not a chronology of our work; field-note/opinion/case-study may use first person only when verified evidence and the reader job justify it. Never present the author as AI. Separately resolve the distribution form with `bash ARTICLE_ROOT_PLACEHOLDER/scripts/resolve-form.sh --card <card_path>` (no card means `article`). Distribution form controls length and exits only; it must never override editorial_form. For `article`, continue below. For `xpost`, write one short hook to its target length and stage only to X. For `ebook`, run the same gates per chapter and report its pending exit without fabricating publication.

STEP 1.5 (APPLY THE PRESELECTED CANDIDATE ONLY TO ITS MATCHED ROUTE): if STEP 0.8 returned CANDIDATE_CANARY and STEP 1 produced topic-route-input.json whose reader.job exactly equals the assignment reader_job, read the immutable strategy_path and apply only its one returned rule to BOTH JA and EN drafts. This is one bounded canary, not an active learned rule: it never mutates reference/learned-playbook.md and never overrides safety, citations, identity, exact8, price, platform, schedules, providers, destination accounts, or recovery. If the exact reader job did not match, do not apply or record the candidate. STEP 4.85 requires excerpts that software can locate in both frozen drafts.

STEP 2 (RESEARCH -- do the real work, no shortcuts): research the topic properly. Use firecrawl (`firecrawl scrape <url> markdown`) for web sources, context7 (`npx ctx7@latest library <name>` then `npx ctx7@latest docs <libraryId> <query>`) for any library/SDK/API docs, and agent-reach/WebSearch for broader discovery. If the topic is a tool, repo, or product, actually RUN it end-to-end yourself and observe the real behavior -- a claim you have not personally verified must not go in the article. Form an honest verdict (should someone use this, who for) grounded in what you actually observed, not marketing copy.

STEP 3 (WRITE, BOTH LANGUAGES, NATIVELY): REQUIRED READ -- before drafting either language title, read ~/profitable-claude/skills/writing-craft/CRAFT.md and ~/profitable-claude/skills/writing-craft/formats/article.md in full, then read ARTICLE_ROOT_PLACEHOLDER/SKILL.md section "執筆プロセス standard" and ARTICLE_ROOT_PLACEHOLDER/reference/title-best-practices.md in full, and obey all four for BOTH the ja title and the en title, not just one. That reference file is the ONLY source of title rules and this prompt adds none of its own. Its section 1 holds real titles with their real engagement numbers, and its section 2 is the ONLY list of bans -- read it there and apply exactly what it says, no more. Do not restate those bans here or anywhere else and do not derive extra ones from them: every past failure of this step came from a ban being paraphrased into something stricter than the measured original. Abstraction, negation and first person are all rewarded patterns in section 1, so never reject a candidate for being abstract, negative or personal. A number in the headline is NOT required and never breaks a tie; when two candidates are close, take the one carrying tension or reversal, not the one carrying digits. Produce at least five candidates per language spread across different section 1 patterns, then record all of them, chosen and rejected, with python3 ARTICLE_ROOT_PLACEHOLDER/scripts/title_candidates.py record --json - --run-dir <this runs record directory> --lang <ja or en>. That recorder refuses a rejection reason stated in rulebook words instead of reader-side words, and refuses a rejection that does not cite the file and line of the rule it applied, so a later pass can score the rejected candidates against real titles and put the loss on the exact line that caused it. If that recorder exits nonzero, read its message, fix the ledger and run it once more; if it still refuses, save the raw ledger JSON as this runs gates/title-candidates-<lang>.raw.json and CONTINUE. Recording is measurement, never a permission to publish -- the PUBLISH ANYWAY boundary above outranks it, and a run that shipped nothing because a ledger would not validate is a worse outcome than a run with one missing measurement. Now write the article in Japanese AND English, each written NATIVELY in that language (not translated from the other -- natural phrasing, idioms, and structure for each language independently). Use the hamburger template documented in the writer-agent skill. Invoke the stop-ai-slop-jp skill on the Japanese draft and fix everything it flags (zenkaku dashes, AI pet phrases, missing subject, thesis-style H2s, false balance, uniform rhythm). Then read ARTICLE_ROOT_PLACEHOLDER/vendor/writing-skills/humanizer/SKILL.md and apply its final humanize pass to BOTH language drafts (bakeoff-verified final filter). Then, for EACH language draft, run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/_shared/citation-strip.py --in-place --report <f> (SKILL.md rule 26: every inline (出典: [label](url)) citation collapses into ONE final 出典/Sources block, deduplicated by URL -- this is mechanical, not a judgment call, so run it as a normalization pass before any gate reads the draft). From the same research, write one independent Japanese short-form X Post into this run as x-post-ja.txt; it is not a summary headline, not an English post, and it remains immutable with the two article drafts. Read ARTICLE_PRODUCT_LANDING_URL and ARTICLE_PRODUCT_ID from the inherited environment. Every one of article-ja.md, article-en.md, and x-post-ja.txt must contain exactly one measurable self-hosted product CTA built from ARTICLE_PRODUCT_LANDING_URL with query keys product_id=ARTICLE_PRODUCT_ID, run_id=RUN_DIR_PLACEHOLDER, artifact_id, variant_id, and click_id. Use artifact_id=article-ja, article-en, and x-post-ja respectively; use three distinct click_id values RUN_DIR_PLACEHOLDER-article-ja, RUN_DIR_PLACEHOLDER-article-en, and RUN_DIR_PLACEHOLDER-x-post-ja; derive each variant_id from the selected title/post variant rather than reusing one value. Build the query with Python urllib.parse.urlencode so values are encoded. Substack, note, or GitHub links remain distribution/citations and do not satisfy this conversion CTA. Generate or select the headline image exactly once with the existing image workflow and save its final bytes as this runs headline-image.png. Author at least one Mermaid-backed explanatory diagram, save its source as this runs body-diagram.mmd, and render its cross-platform PNG once as this runs body-diagram.png (additional body assets use body-<name>.png). Before any gate or publication init reads the final draft, make article-en.md itself begin with YAML frontmatter containing the selected non-empty title and 1-4 non-empty Dev.to tags; never defer this metadata to a platform adapter. Then run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/canonical_media.py attach --file <draft> for article-ja.md and article-en.md, then run the same command with validate for each draft. Each canonical draft must contain exactly one selected headline-image.png reference, the Mermaid source, and exactly one body-diagram.png reference. Both native drafts reuse those same immutable media bytes. Never regenerate these media after publication state initialization.

STEP 3.5 (OPERATOR-IDENTIFIER BAN -- HARD, enforced in code at publish time by scripts/pii-gate.py): write as the AI persona only. Nothing you publish -- body, title, alt text, the 出典/Sources block, the CTA, x-post-ja.txt -- may name the operator (real name, personal GitHub/X/note/Substack handle, personal repo URL, personal email, phone) or give a city-, region- or address-level location for the machine this runs on (東京の…, in Tokyo, a 〒 code, an office). Saying you ran it on your own always-on machine is fine; saying where that machine sits, or who owns it, is not. This bites hardest in 出典/Sources: when the only evidence for a claim lives in a personal repository, describe in prose what that evidence shows, or cite the public product/docs URL -- never paste a https://github.com/<operator-handle>/... link, and never use an operator-owned repo as an exemplar URL. A claim whose only citation would name the operator ships without that link, or does not ship. scripts/pii-gate.py blocks publication on any hit and fails the run, so obfuscating an identifier is not a fix -- remove it.

STEP 4 (QUALITY IMPROVEMENT -- mandatory attempts): for EACH language draft run language-purity-gate.sh, seo-gate.sh, bookmark-gate.sh, and freshness-gate.sh with their documented arguments. Fix concrete findings within the STEP 0.7 budget and record raw failures honestly. These deterministic checks inform revision; editorial and reader terminal receipts in STEPS 4.5/4.7 decide whether the artifact may freeze.

STEP 4.5 (COMPOSITE EDITOR): for EACH language run scripts/editorial-gate.sh, apply at most ONE revision pass from its prioritised fixes, then run it once more on the revised bytes when needed. Persist gates/editorial-<lang>.json with the honest verdict and current article_sha256. rubric/deslop/eval remain replaced and must not run here. A FAIL, malformed verdict, timeout, or 429 is honest non-PASS evidence consumed by STEP 4.75; never fabricate PASS.

STEP 4.6 (IDENTITY SAFETY): for EACH language draft run identity-gate.sh. Do NOT run rubric-judge.sh, deslop-gate.sh or eval-gate.sh here -- STEP 4.5 replaced all three with the one composite editor, and running them anyway is what kept the replacement dead. Identity findings about secrets/private/internal leakage or fabricated track record are safety blockers: fix them; if they remain, route this topic through STEP 4.8s alternate-topic path. If identity-gate itself crashes, times out, returns malformed output, or returns 429, use record-quality-advisory.py to append raw output, gate=identity, language, attempt, and exit code under this runs gates/, then continue to STEP 4.7 with the best current draft. Never write a quality carry-over ledger row and never label infrastructure failure PASS. Identity is safety, so a real identity FAIL is never advisory.

STEP 4.7 (STABLE READER TESTING): for EACH language use one immutable gates/reader-questions-<lang>.json and pass it through --questions-file on every invocation. Run one initial evaluation and at most two revise-then-evaluate cycles. The terminal gates/reader-testing-gate-<lang>.terminal.json must say status=pass, payload.verdict=PASS, and article_sha256 equal to the current draft. Remaining questions, crash, timeout, malformed output, or 429 are non-PASS evidence for STEP 4.75. Never regenerate questions or fabricate PASS.

STEP 4.75 (QUALITY ADVISORY -- mandatory): run `python3 ARTICLE_ROOT_PLACEHOLDER/scripts/quality_self_heal.py assess --run-dir "$ARTICLE_RUN_DIR" --draft-ja "$ARTICLE_RUN_DIR/article-ja.md" --draft-en "$ARTICLE_RUN_DIR/article-en.md"` once. Editorial/reader FAIL must immediately return action=force_publish_advisory and must never delay shipment. Continue to publication when identity, conscience, PII, duplicate, media, CTA, monetization, and platform guards are PASS; keep editorial/reader failures visible in the Telegram report. Never reuse stale identity/safety evidence.

STEP 4.8 (CONSCIENCE GATE -- mandatory, quality-independent, fail-closed, NO-SKIP): after STEP 4.75, run bash ARTICLE_ROOT_PLACEHOLDER/scripts/conscience-gate.sh <f> --lang <ja|en> for BOTH drafts regardless of editorial/reader verdict. This fresh, context-zero judge sees only the proposed article and returns ALLOW or BLOCK for gray-zone exposure: internal credentials, non-public information about others, reputational or legal risk, or identifying a real person/organization in order to accuse them. A non-zero exit, missing JSON, or BLOCK is publication-blocking and MUST NOT be bypassed or softened. If either language is BLOCKed, publish neither version of that topic today: append an honest carry-over row for the blocked topic to ARTICLE_STATE_DIR_PLACEHOLDER/articles.jsonl with published:false and state:\"carry-over:conscience-block:<reason>\", preserve its research and drafts for the next day, and make the carry-over mechanically selectable tomorrow. If this run claimed a card, move that card from ARTICLE_STATE_DIR_PLACEHOLDER/topics/in-progress/ back to ARTICLE_STATE_DIR_PLACEHOLDER/topics/queue/ without changing created/priority, remember its basename, and pass one --exclude-basename argument for it on every same-pass STEP 1 restart. If the topic came from fallback state, append an explicit carry-over to ARTICLE_STATE_DIR_PLACEHOLDER/topic-queue.md and materialize it as a normal ARTICLE_STATE_DIR_PLACEHOLDER/topics/queue/ card with original topic, research paths, created timestamp, and stable basename; exclude that basename for the rest of this pass. Materialize a genuinely different alternate topic as another normal queue card, then restart through select-next-topic.sh. Both the blocked carry-over and alternate must use queue cards so the selector cannot return the blocked topic today and can return it on the next scheduled pass when exclusions reset. The no-skip invariant applies to the daily publishing obligation, never to forcing a gray-zone topic through this gate. A pass may not proceed to STEP 5 without conscience-gate.sh ALLOW for BOTH ja and en.

STEP 4.85 (ACTIVE STRATEGY CONSUMPTION AND CANDIDATE CANARY EVIDENCE -- advisory to publishing, mandatory for learning): after copying the final JA/EN drafts into this runs article-ja.md and article-en.md, if STEP 0.8 read slice=writer-learning, write gates/writer-learning-consumption.json with exactly {"experiment_id":"<learning_cycle_id>","strategy_sha256":"<consumed_hash>","changed_field":"<manifest changed_field>","excerpts":{"ja":"<one exact 12+ character excerpt in article-ja.md demonstrating that active rule>","en":"<one exact 12+ character excerpt in article-en.md demonstrating that active rule>"}}. Run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/writer_learning_worker.py record-consumption --skill-dir ARTICLE_ROOT_PLACEHOLDER --run-dir ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER --evidence ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER/gates/writer-learning-consumption.json. The worker independently rechecks the wrapper receipt, active pointer, weight file hash, experiment, both exact excerpts, and both frozen article hashes before recording consumption. If STEP 1.5 returned status CANDIDATE_CANARY, separately write gates/writer-learning-canary.json with exactly {"experiment_id":"<returned id>","excerpts":{"ja":"<one exact 12+ character excerpt from article-ja.md that demonstrates the one candidate rule>","en":"<one exact 12+ character excerpt from article-en.md that demonstrates the one candidate rule>"}}. Run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/writer_learning_worker.py record-application --skill-dir ARTICLE_ROOT_PLACEHOLDER --run-dir ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER --evidence ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER/gates/writer-learning-canary.json. The worker requires the frozen matched reader_job, verifies both excerpts are byte-present, binds the candidate strategy hash to both frozen artifact hashes exactly once, and consumes the assignment so later runs cannot apply it again. If either learning receipt fails, preserve its real error under this runs gates and continue publishing the last-known-good article; absent proof never fabricates learning and must not block todays exact8.

STEP 4.86 (CTA + X POST INVARIANTS -- mandatory, revenue-path, fail-closed): run bash ARTICLE_ROOT_PLACEHOLDER/scripts/cta-gate.sh article-ja.md --run-id RUN_DIR_PLACEHOLDER --artifact-id article-ja, then the same for article-en.md/artifact-id article-en and x-post-ja.txt/artifact-id x-post-ja. Save each JSON result as gates/cta-ja.json, gates/cta-en.json, and gates/cta-x-post-ja.json. A missing or mismatched measurable product landing path is not an advisory quality miss: revise that artifact before proceeding. x-post-ja.txt must contain 1..280 characters after stripping outer whitespace; shorten it before freeze rather than letting the pending worker reject the same immutable intent forever. publication_resume.py init independently reruns the CTA gate on all three frozen artifacts, requires distinct click_id values, enforces the X Post length, and refuses to create publication state if any result is not PASS, so no staging or live action can precede the invariant.

STEP 4.9 (PUBLICATION SSOT -- mandatory before staging or live action): export ARTICLE_RUN_DIR=ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER, ARTICLE_PUBLICATION_STATE=$ARTICLE_RUN_DIR/gates/publication-state.json, and ARTICLE_LEDGER=ARTICLE_STATE_DIR_PLACEHOLDER/articles.jsonl. Keep all three exported through every staging, guard, live, and reconcile command. Copy the final drafts and x-post into this run and require canonical media. After conscience ALLOW has caused quality-phase-terminal.py to write hash-bound identity=PASS and safety=ALLOW markers for both languages, run `python3 ARTICLE_ROOT_PLACEHOLDER/scripts/publication_resume.py --state "$ARTICLE_PUBLICATION_STATE" --ledger "$ARTICLE_LEDGER" init --run-id RUN_DIR_PLACEHOLDER --run-dir "$ARTICLE_RUN_DIR" --topic-id <shared_topic_id> --draft-ja "$ARTICLE_RUN_DIR/article-ja.md" --draft-en "$ARTICLE_RUN_DIR/article-en.md" --x-post-ja $ARTICLE_RUN_DIR/x-post-ja.txt --headline-image "$ARTICLE_RUN_DIR/headline-image.png" --body-asset "$ARTICLE_RUN_DIR/body-diagram.png" --safety ALLOW --max-resume-attempts 2 --require-quality` and append one --body-asset for every additional canonical body asset. Initialization accepts editorial/reader ADVISORY when the current quality receipt proves `force_publish_after_iterations=1` and continuous policy; it still rejects missing/stale identity, conscience, safety, PII, CTA, media, duplicate, monetization, or platform proofs. After successful init publish the immutable run media receipt, then stage only the four active destinations. Never replace run/topic/artifact identities.

STEP 5 (STAGE EXACTLY FOUR ACTIVE ARTICLE DESTINATIONS -- every pass): use the REAL orchestrator ARTICLE_ROOT_PLACEHOLDER/scripts/run.sh, which you should read first (case "$CHANNEL" arms) so you use its real flags. Put exactly four platform/language jobs below in a JSONL manifest at ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER/gates/platform-dispatch.jsonl: note/ja, substack/ja, substack/en, and x-article/ja. Each line is exactly {"platform":"...","lang":"ja|en","argv":["one","argument","per","element"],"env":{"NAME":"value"}}. Build it with JSON serialization, never a shell command string: a title such as "$44M / $188K" must be one literal argv element and must not be shell-expanded. Resolve every argv path to an absolute path first because argv does not expand ~, $HOME, or globs. Put ARTICLE_QUALITY_ADVISORY, ARTICLE_QUALITY_ADVISORY_LOG, ARTICLE_RUN_DIR, ARTICLE_PUBLICATION_STATE, ARTICLE_LEDGER, and ARTICLE_PUBLISH_PAIR in the matching env. For the X Article JA row, put X_COVER equal to the same existing absolute cover PNG and ARTICLE_PUBLISH_PAIR=x-article/ja; for Substack put ARTICLE_PUBLISH_PAIR=substack/ja or substack/en so its first draft ID becomes the stable target. Then run platform-dispatch.sh --manifest <that file> --results ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER/gates/platform-dispatch-results.jsonl. The helper preflights the complete manifest, rejects shell -c/background dispatch, waits for each foreground argv to finish, executes every row after earlier non-zero results, and returns non-zero only after recording all four rows:
  - note (ja):       ARTICLE_QUALITY_ADVISORY=1 ARTICLE_QUALITY_ADVISORY_LOG=ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER/gates/publish-quality-advisory-ja.log bash ARTICLE_ROOT_PLACEHOLDER/scripts/run.sh --channel note --phase publish --markdown-file <ja.md> --title "<t>" --meta "<m>"
  - Substack ja:     ARTICLE_QUALITY_ADVISORY=1 ARTICLE_QUALITY_ADVISORY_LOG=ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER/gates/publish-quality-advisory-ja.log bash ARTICLE_ROOT_PLACEHOLDER/scripts/run.sh --channel substack-ja --phase publish --markdown-file <ja.md> --title "<t>" --meta "<m>"
  - Substack en:     ARTICLE_QUALITY_ADVISORY=1 ARTICLE_QUALITY_ADVISORY_LOG=ARTICLE_STATE_DIR_PLACEHOLDER/runs/RUN_DIR_PLACEHOLDER/gates/publish-quality-advisory-en.log bash ARTICLE_ROOT_PLACEHOLDER/scripts/run.sh --channel substack-en --phase publish --markdown-file <en.md> --title "<t>" --meta "<m>"
  - X Article ja:   argv ["bash","ARTICLE_ROOT_PLACEHOLDER/scripts/x-publish/publish-to-x.sh","publish","<absolute-ja.md>","--mode","draft","--lang","ja"] with env.X_COVER set to the same existing absolute cover PNG (draft mode only -- never call the `go` subcommand)
A pass that stages fewer or more than these four rows is a FAILED pass. Every draft creation is subject to the shared CDP :9222 daily-driver browser lock this driver script already holds around this whole model invocation, so no extra locking is needed inside your own actions.

STEP 5.5 (NOTE EYECATCH -- mandatory, no note draft ships without one): immediately after the note dispatch above returns its "DRAFT (unpublished) key=<KEY> ..." line, extract <KEY>. Require $X_COVER to resolve to this immutable runs $ARTICLE_RUN_DIR/headline-image.png and require its SHA-256 to equal publication-state.json media.headline_image.sha256. Copy that exact file to the fixed path the script reads: `mkdir -p ARTICLE_STATE_DIR_PLACEHOLDER/note-work && cp "$ARTICLE_RUN_DIR/headline-image.png" ARTICLE_STATE_DIR_PLACEHOLDER/note-work/thumb.png`. Never generate, select, or substitute another cover. Then run: `NOTE_KEY=<KEY> python3 ARTICLE_ROOT_PLACEHOLDER/scripts/note-publish/set-eyecatch-draft.py`. This script only sets the draft eyecatch and stops -- it has no code path that can publish. READBACK (mandatory, do not skip): the script itself re-reads the DOM after setting the image and prints "EYECATCH_IN_EDITOR: <src>" -- treat this as success ONLY if <src> is a real assets.st-note.com URL, not "NONE"; also open the screenshot it saves to ARTICLE_STATE_DIR_PLACEHOLDER/note-work/eyecatch-draft-set.png as a second own-eyes check before deciding this succeeded. If the eyecatch genuinely fails to set (EYECATCH_IN_EDITOR: NONE after a real attempt), report note as a failed step in STEP 8/9 with the real error -- never mark note as fully succeeded without a confirmed EYECATCH_IN_EDITOR hit.

STEP 6 (OWN-EYES VERIFY -- mandatory, a 200 from a tool is not evidence): for EVERY draft URL you get back, open it yourself in the already-logged-in daily-driver Chromium (CDP :9222, e.g. `agent-browser --auto-connect` or the camofox/CloakBrowser tooling already configured on this machine) and confirm ON THE REAL RENDERED PAGE that (a) the draft genuinely exists and (b) it is NOT publicly live (still shows as a draft/unpublished in the editor UI). Do not accept an HTTP status code alone as proof -- look at the page. If a draft looks public, STOP, do not report success for that platform, and flag it as a safety concern in your Telegram report.

STEP 6.5 (RENDER-VERIFY -- mandatory, a fresh independent vision judge per platform, catches what STEP 6 own-eyes cannot): STEP 6 is YOUR OWN judgment on the same draft you just wrote, which is exactly the kind of self-graded check this loop keeps failing (same reason deslop-gate.sh/eval-gate.sh use a fresh model call instead of trusting the writer). For EVERY active platform whose draft URL supports it (note, substack-ja, substack-en -- skip X, it has no equivalent editor URL), run bash ARTICLE_ROOT_PLACEHOLDER/scripts/render-verify-draft.sh --platform <note|substack> --url <the draft edit URL from STEP 5> --lang <ja|en>. It takes a real full-page screenshot and a fresh vision judge checks it for rendering defects your own eyes tend to miss: raw frontmatter text leaking into the visible body, a broken-image icon or literal unrendered ```mermaid text where a figure should be. verdict:"FAIL" is BLOCKING (STEP 4.5s rule applies here too -- a script crash with no verdict JSON is ALSO blocking, never treat it as this platform is just fine today). On FAIL: read the problems list, fix only runtime draft content and re-run render-verify-draft.sh -- up to 3 total attempts for that platform. If the failure is in tracked source or still FAILs after 3 attempts, persist the exact blocker and concrete fix hint under the current run gates, leave that pair pending, and continue remaining independent pairs. Never spawn a source fixer or mark a platform done on a render-verify FAIL you did not resolve.

STEP 7 (LEDGER -- one honest STAGING row per active destination, never fabricated): choose one stable topic_id shared by both language versions. Append exactly four rows, one for each active destination, with "run_id":"RUN_DIR_PLACEHOLDER", that shared topic_id, localized topic, platform, lang, draft_url or null, exact state, verified_logged_in, published:false, and the frozen topic_source/editorial_form from gates/topic-route.json. In armed mode STEP 19 later appends published:true rows only after each real go-live and reality-gate PASS. Never invent a URL or verification. If a card was claimed, move it from ARTICLE_STATE_DIR_PLACEHOLDER/topics/in-progress/ to ARTICLE_STATE_DIR_PLACEHOLDER/topics/done/ only after every attempt is recorded.

STEP 8 (PLATFORMS ARE INDEPENDENT): if one platform fails (auth expired, selector changed, rate limit, etc), stage every other platform anyway and record the failed platform for retry. If the root cause is a tracked-source defect, record it under the current run gates and leave that pair pending for a reviewed source deployment. Never edit source, fake a URL, or let one platform short-circuit another.

STEP 9 (REPORT EVIDENCE -- MANDATORY, every pass, success or failure): persist honest evidence containing the topic chosen, all four active destination draft URLs (or the honest failure reason for any that failed), and what you personally verified on each page. Do not invoke a gateway or Telegram CLI: after the model exits, the repository-owned article-completion-notify.py sends the durable receipt for this run through the shared Life Manager Telegram transport and records the real messageId. The compatibility x-post artifact may be retained for the nonpublication CTA gate, but it is not a destination row or publication work. In unarmed mode, record that these are DRAFTS awaiting manual publish and never live. In armed mode, STEP 20 replaces that reminder with immediate live and scheduled-pending evidence.

STEP 10 (FINISH -- HONEST DELIVERY): completion requires identity safety clear, conscience ALLOW, every active platform attempted independently, and exact current-run ledger evidence. Editorial/reader FAIL is retried in the same run up to five iterations; after the fifth it may be an explicitly recorded force-publish advisory, never a hidden bypass. In armed mode article-run-complete.py requires four active live reality receipts; the four dormant skip receipts are not failures or SLO work. Until then report PENDING; never equate foreground exit with shipped.'

# task #27: PROMPT is single-quoted (the literal text above can't be touched safely -- it is a
# live production agent instruction, editing it in place risks corrupting it), so the Telegram
# ID is swapped in via a plain string substitution on the already-built value instead of
# interpolating a variable into the quoted literal. No-op (identical string) unless
# TELEGRAM_TARGET_ID is overridden from the default set above.
PROMPT="${PROMPT//8547730585/$TELEGRAM_TARGET_ID}"
# The archived prompt text names the former standalone craft tree for compatibility with old
# runs. Resolve those instructions to the immutable Life Manager release before the model sees
# them, so a fresh run never reads outside ARTICLE_ROOT.
LEGACY_WRITING_CRAFT_ROOT="$HOME/$(printf 'profitable-%s' 'claude')/skills/writing-craft"
LEGACY_WRITING_CRAFT_LITERAL_ROOT='~/profitable-claude/skills/writing-craft'
LEGACY_CRAFT_FILE="${LEGACY_WRITING_CRAFT_ROOT}/CRAFT.md"
LEGACY_ARTICLE_FORMAT_FILE="${LEGACY_WRITING_CRAFT_ROOT}/formats/article.md"
LEGACY_LITERAL_CRAFT_FILE="${LEGACY_WRITING_CRAFT_LITERAL_ROOT}/CRAFT.md"
LEGACY_LITERAL_ARTICLE_FORMAT_FILE="${LEGACY_WRITING_CRAFT_LITERAL_ROOT}/formats/article.md"
CURRENT_CRAFT_FILE="$ARTICLE_ROOT/reference/CRAFT.md"
CURRENT_ARTICLE_FORMAT_FILE="$ARTICLE_ROOT/reference/formats/article.md"
# Replace both expanded and literal legacy paths. The prompt is single-quoted above, so
# parameter expansion is the last safe compatibility boundary before the immutable prompt file.
PROMPT="${PROMPT//$LEGACY_CRAFT_FILE/$CURRENT_CRAFT_FILE}"
PROMPT="${PROMPT//$LEGACY_ARTICLE_FORMAT_FILE/$CURRENT_ARTICLE_FORMAT_FILE}"
PROMPT="${PROMPT//$LEGACY_LITERAL_CRAFT_FILE/$CURRENT_CRAFT_FILE}"
PROMPT="${PROMPT//$LEGACY_LITERAL_ARTICLE_FORMAT_FILE/$CURRENT_ARTICLE_FORMAT_FILE}"
PROMPT="${PROMPT//PUBLICATION_PAUSE_SNAPSHOT_PLACEHOLDER/$PUBLICATION_PAUSE_SNAPSHOT}"
# self-heal L2 (spec #22): append-only, same technique as above -- if ensure_browser.sh could
# not bring the shared daily-driver back, tell the pass to degrade gracefully (skip the
# browser-dependent platforms and report why) instead of failing blind on every step that
# touches it. No-op (PROMPT unchanged) when the browser is ALIVE or was RECOVERED, the normal
# case every day so far.
if [ "$BROWSER_STATUS" != "ALIVE" ] && [ "$BROWSER_STATUS" != "RECOVERED" ]; then
  PROMPT="${PROMPT}"'

★ BROWSER UNAVAILABLE THIS RUN ★ ensure_browser.sh could not bring the shared CDP :9222
daily-driver back before this pass started (status: '"$BROWSER_STATUS"'). Any platform whose
draft-staging or own-eyes verification drives that browser (substack, x) will not work
today -- treat each as a failed platform per STEP 8, with "browser unavailable" as the honest
reason, and do not attempt a verification you cannot actually perform. note publishing goes
through its own separate session, not this browser, so attempt it normally. The research and
writing steps (1-4) are unaffected either way -- still do the real work.'
fi

# ARTICLE_AUTOPUBLISH kill-switch (Dais decision #41, 2026-07-16, spec docs/superpowers/specs/
# 2026-07-14-article-earn-loop-ssot.md PART J/J2): default 0/absent = today's draft-only
# behavior, byte-identical PROMPT, zero change. Only when the plist injects
# ARTICLE_AUTOPUBLISH=1 does the addendum below get appended to PROMPT, authorizing this pass
# to take its own active-four package live under the persisted schedule. Rollback = flip the plist
# env var back to 0/absent, one line, no code change.
AUTOPUBLISH="${ARTICLE_AUTOPUBLISH:-0}"
if [ "$AUTOPUBLISH" = "1" ]; then
  PROMPT="${PROMPT}"'

★★★ ARTICLE_AUTOPUBLISH=1 IS SET FOR THIS RUN. THIS run is ARMED for one active-four package. After all immutable artifacts, identities, and gates exist, publish exactly these four active destinations independently and in this exact order: note/ja, substack/ja, substack/en, and x-article/ja. A failure continues to the next pair. Before any live side effect, require the platform-dispatch manifest and results to contain exactly these four rows, and record four durable dormant skip receipts through publication-guard.py register-dormant-skip, including x-article/en and x-post/ja. Do not stage, create an intent, repair, or publish any dormant destination. The run remains PENDING until all four active reality-PASS rows exist. Never use an older draft, prior run, replacement target, or a second X Post. ★★★

STEP 11 (CAPTURE ALL STABLE IDENTITIES): extract the note key, separate Substack JA/EN draft IDs, and the X Article JA draft URL from the exactly four staging results. Keep the compatibility x-post artifact outside the publication manifest and do not create a target for it.

STEP 11.5 (REGISTER EXACTLY FOUR ACTIVE TARGETS AND FOUR DORMANT SKIPS BEFORE THE FIRST LIVE SIDE EFFECT): with the STEP 4.9 ARTICLE_RUN_DIR, ARTICLE_PUBLICATION_STATE, and ARTICLE_LEDGER still exported, use `python3 ARTICLE_ROOT_PLACEHOLDER/scripts/publication-guard.py register-intent` exactly once for each of note/ja, substack/ja, substack/en, and x-article/ja with their stable targets from the four staging results. Then use `python3 ARTICLE_ROOT_PLACEHOLDER/scripts/publication-guard.py register-dormant-skip` exactly once for each of the four dormant pairs with reason=dormant-destination. Never call publication_resume.py intent directly from a managed pass. Exact-repeat is idempotent. Run `python3 ARTICLE_ROOT_PLACEHOLDER/scripts/publication-guard.py plan` and require resumable:true with exactly four active intents plus four explicit skip receipts; dormant skip receipts are excluded from pending work. A missing, conflicting, or ambiguous target means no live side effect.

STEP 12 (FIXED MONEY CONTRACT + TAGS): run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/note-publish/note_monetization_policy.py desired-state and require the JSON to say access_model=one_time_purchase, currency=JPY, price_minor=500, paywall_required=true, publisher_args=["--price","500"]. This executable desired-state contract is authoritative for every newly published note article; article count, follower count, and price-check suggestions cannot switch it to free or change the price. Separately run bash ARTICLE_ROOT_PLACEHOLDER/scripts/_shared/tag-counts.py <6-8 candidate hashtag words for this topic, no leading #> and pick up to 5 from the returned counts, avoiding any tag whose count is in the hundreds of thousands (it will bury this article).

STEP 13 (NOTE JP -- ¥500 go live -- this is the ONLY command in this entire loop that actually clicks the publish button on note.com): immediately before the publish-paid.py attempt, run bash ARTICLE_ROOT_PLACEHOLDER/scripts/note-publish/publish-to-note.sh enable-publish to create the 10-minute sentinel; treat it as single-use for that attempt and never reuse it for a retry. Then run NOTE_MODE=go WRITER_BROWSER_PYTHON_PLACEHOLDER ARTICLE_ROOT_PLACEHOLDER/scripts/note-publish/publish-paid.py --key <KEY from STEP 11> --price 500 --after-chars <your own editorial judgment of where the useful free preview ends and the paid material begins, in characters> --tags "<up to 5 tags from STEP 12, comma-separated, no leading #>" --arm. Require exit code 0 plus PAID_PUBLISHED verified=true and API_VERIFY price=500 before treating note as live. --free is outside this Writer money contract.

STEP 14 (NOTE CONVERSION PREVIEW, ja only): generate the Japanese free-preview derivative for any adapter that explicitly consumes a note conversion artifact (make-free-version.py hardcodes a Japanese paywall footer, so it has no English equivalent): run bash ARTICLE_ROOT_PLACEHOLDER/scripts/_shared/make-free-version.py --markdown-file <the ja.md from STEP 3> --note-url <the live note URL from STEP 13> --price 500 --paid-contents "<your own exact naming of what is behind the paywall>" --summary-file <a small file you write yourself with 3-5 honest summary bullets, no slop> --out <a free.md path> --after-chars <your own editorial judgment, independent of the note paywall line in STEP 13>. Never silently substitute this derivative for the immutable source article; a destination adapter must name it explicitly. The four active destinations consume only their own immutable staging rows.

STEP 15 (DORMANT DESTINATIONS -- skip-only boundary): do not stage, create intents, run adapters, invoke publishers, or perform repairs for any dormant destination. Persist only the four explicit durable skip receipts; they never count as pending work, a live row, or an SLO breach.

STEP 17 (SUBSTACK JA THEN EN -- paid subscriber contract, immediate group items 4 and 5, no subscriber email): enable the sentinel for each language, then run with ARTICLE_PUBLISH_PAIR=substack/ja SUBSTACK_MODE=go for Japanese and ARTICLE_PUBLISH_PAIR=substack/en SUBSTACK_MODE=go for English. The script reuses each persisted draft ID and reads authenticated GET /api/v1/drafts/{id} before POST, so a resumed run never creates or publishes a replacement. Require that readback to prove audience=only_paid, should_send_free_preview=true, and exactly one paywall node before either live side effect. send:false is mandatory.

STEP 18 (X ARTICLE JA -- immediate group item 6): run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/x-publish/x_inplace_repair.py --pair x-article/ja. This command opens only the persisted edit URL, requires its title to match the immutable Japanese artifact before preflight, replaces its body and media from that artifact, and publishes that same edit ID; never use the generic new-article composer or a replacement target here. The mandatory guard skips an already-live pair. Capture the remote publication timestamp from authenticated readback in its record-live evidence. Do not run x-article/en here.

STEP 19 (REALITY + LEDGER RECONCILIATION): immediately after EACH of the four active go-live operations, run the destination real reality gate and record-live only when authenticated/public readback proves the exact stable identity, public ID, live URL, publication timestamp, artifact hash, language, body, and required media. Never append a live row by hand. A publish-success/ledger-crash is repaired from remote reality without a second publish side effect. Run publication_resume.py worker-plan: any failed active pair remains independently pending; dormant skip receipts never enter pending or failure/SLO accounting. article-run-complete.py succeeds only at all four active reality receipts.

STEP 20 (PENDING RECEIPT): send an honest Telegram pending receipt with the run ID, every active live URL and reality verdict, every failed active pair, and the four durable dormant skip receipts. Do not call it shipped and do not send an active-four success receipt until article-run-complete.py passes with all four verified rows.'
fi

PROMPT="$PROMPT

TELEGRAM REPORT LANGUAGE: every Telegram progress or delivery message sent by this loop must be neutral natural language that a non-technical family member can understand. Do not add harness labels or prefixes such as Codex::: or Claude:::; start directly with the report.

MEDIA CREATE-ONCE OVERRIDE (mandatory; supersedes STEP 3's direct-save wording): the wrapper has already armed an immutable two-asset boundary. Never pass canonical headline-image.png or body-diagram.png directly to an image generator or renderer. Create every output under \$ARTICLE_RUN_DIR/gates/media-candidates/ with a distinct candidate filename. For the headline, write one concise article-specific illustration prompt to \$ARTICLE_RUN_DIR/gates/headline-image-prompt.txt and one honest alt text to \$ARTICLE_RUN_DIR/gates/headline-image-alt.txt. Do not use ImageMagick, SVG, a reusable series cover, ChatGPT web image generation, or any model-selected image path. Run exactly once: python3 ARTICLE_ROOT_PLACEHOLDER/scripts/gpt_image_headline.py generate --prompt-file \"\$ARTICLE_RUN_DIR/gates/headline-image-prompt.txt\" --alt-file \"\$ARTICLE_RUN_DIR/gates/headline-image-alt.txt\" --candidate \"\$ARTICLE_RUN_DIR/gates/media-candidates/headline-gpt-image-2.png\" --intent \"\$ARTICLE_RUN_DIR/gates/headline-image-api-intent.json\" --receipt \"\$ARTICLE_RUN_DIR/gates/headline-image-api-receipt.json\". It requires the exact model gpt-image-2-2026-04-21 and refuses to resend an unknown request. Commit only that candidate with python3 ARTICLE_ROOT_PLACEHOLDER/scripts/media_create_once.py commit --candidate \"\$ARTICLE_RUN_DIR/gates/media-candidates/headline-gpt-image-2.png\" --destination \"\$ARTICLE_RUN_DIR/headline-image.png\" --receipt \"\$ARTICLE_RUN_DIR/gates/headline-image-create.json\" --kind headline. Then run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/gpt_image_headline.py verify --candidate \"\$ARTICLE_RUN_DIR/gates/media-candidates/headline-gpt-image-2.png\" --receipt \"\$ARTICLE_RUN_DIR/gates/headline-image-api-receipt.json\". Render one body diagram candidate whose projected height at X's 587px content width is between 110px and 650px (for a 1300px-wide source, roughly 244–1440px tall), then run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/media_create_once.py commit --candidate <candidate> --destination \"\$ARTICLE_RUN_DIR/body-diagram.png\" --receipt \"\$ARTICLE_RUN_DIR/gates/body-diagram-create.json\" --kind body. A response-loss replay of the exact same commit is allowed; a different second candidate is refused and must never trigger another generator call. Before any gate or publication init, run python3 ARTICLE_ROOT_PLACEHOLDER/scripts/media_create_once.py verify --run-dir \"\$ARTICLE_RUN_DIR\"."

# RUN RECORD (spec 47): replace only while writing a new immutable prompt, after every optional
# addendum has been appended. Bash 3.2's in-memory global replacement becomes superlinear on this
# large armed prompt; one sed stream preserves the exact bytes without delaying the runner.
PROMPT_FILE="$RUN_DIR/article-daily-prompt.txt"
GENERATION_STATE="$ARTICLE_ROOT/scripts/article_generation_state.py"
LEDGER="$STATE_DIR/articles.jsonl"
GENERATION_ARGS=(--run-dir "$RUN_DIR" --run-id "$RUN_TS" --prompt-file "$PROMPT_FILE" --ledger "$LEDGER")
# Quality-gate wrappers use this inherited identity to persist and cap rubric/reader
# evaluations before publication initialization. Export it before the model starts so an
# agent cannot accidentally bypass the per-run attempt controller by delaying STEP 4.9.
export ARTICLE_RUN_DIR="$RUN_DIR"

writer_capacity_preflight() {
  local free_kib flag control_dir="${LIFE_MANAGER_HOST_STATE_DIR:-$HOME/.local/state/life-manager/state}"
  local required_kib="$(( (DISK_LOW_THRESHOLD_BYTES + 1023) / 1024 ))"
  free_kib="$(df -Pk / 2>/dev/null | awk 'NR==2{print $4}')"
  case "$free_kib" in
    ''|*[!0-9]*)
      echo "=== article-daily provider gate BLOCK: disk capacity unavailable ===" >>"$LOG"
      return 78
      ;;
  esac
  case "$required_kib" in
    ''|*[!0-9]*|0)
      echo "=== article-daily provider gate BLOCK: disk floor configuration invalid ===" >>"$LOG"
      return 78
      ;;
  esac
  if [ "$free_kib" -lt "$required_kib" ]; then
    echo "=== article-daily provider gate BLOCK: free=${free_kib}KiB below Life Manager ${required_kib}KiB floor ===" >>"$LOG"
    return 78
  fi
  if [ ! -d "$control_dir" ] || [ ! -r "$control_dir" ] || [ ! -x "$control_dir" ]; then
    echo "=== article-daily provider gate BLOCK: control directory unavailable=$control_dir ===" >>"$LOG"
    return 78
  fi
  for flag in "$control_dir/disk-writers.stop" "$control_dir/disk-pressure.block"; do
    if [ -L "$flag" ] || { [ -e "$flag" ] && [ ! -f "$flag" ]; }; then
      echo "=== article-daily provider gate BLOCK: non-regular control flag=$flag ===" >>"$LOG"
      return 78
    fi
    if [ -f "$flag" ] && {
      { [ "$flag" = "$control_dir/disk-pressure.block" ] \
        && [ "${GIG_IGNORE_DISK_PRESSURE_BLOCK:-}" = "1" ]; } \
      || { [ "$flag" = "$control_dir/disk-writers.stop" ] \
        && [ "${GIG_IGNORE_DISK_WRITERS_STOP:-}" = "1" ]; };
    }; then
      continue
    fi
    if [ -f "$flag" ]; then
      echo "=== article-daily provider gate BLOCK: control flag=$flag ===" >>"$LOG"
      return 78
    fi
  done
  return 0
}
if ! writer_capacity_preflight; then
  exit 78
fi

# Judge broker: nested judge/vision calls inside the bounded agent sandbox cannot
# start a provider process, so this unsandboxed sidecar serves their request files
# from the run-scoped state tree through the same model boundary.
ARTICLE_MODEL_LOG="$LOG" bash "$ARTICLE_ROOT/runtime/judge-broker.sh" "$RUN_DIR" &
JUDGE_BROKER_PID=$!
cleanup_generation_exit() {
  if [ -n "${JUDGE_BROKER_PID:-}" ]; then
    kill -TERM "$JUDGE_BROKER_PID" 2>/dev/null || true
    wait "$JUDGE_BROKER_PID" 2>/dev/null || true
    JUDGE_BROKER_PID=""
  fi
  cleanup_article_locks
}
trap 'cleanup_generation_exit' EXIT
if [ "$RESUME_GENERATION" -eq 1 ]; then
  [ -f "$PROMPT_FILE" ] || {
    echo "=== article-daily generation resume BLOCK: immutable prompt is missing ===" >>"$LOG"
    exit 0
  }
  python3 "$GENERATION_STATE" "${GENERATION_ARGS[@]}" \
    rebind-release --current-root "$ARTICLE_ROOT" >>"$LOG" 2>&1 || {
    echo "=== article-daily generation resume BLOCK: release rebind failed ===" >>"$LOG"
    exit 0
  }
else
  printf '%s' "$PROMPT" \
    | sed \
      -e "s|RUN_DIR_PLACEHOLDER|$RUN_TS|g" \
      -e "s|ARTICLE_ROOT_PLACEHOLDER|$ARTICLE_ROOT|g" \
      -e "s|ARTICLE_STATE_DIR_PLACEHOLDER|$STATE_DIR|g" \
      -e "s|WRITER_LOG_DIR_PLACEHOLDER|$WRITER_LOG_DIR|g" \
      -e "s|WRITER_BROWSER_PYTHON_PLACEHOLDER|$WRITER_BROWSER_PYTHON|g" \
      >"$PROMPT_FILE"
  python3 "$GENERATION_STATE" "${GENERATION_ARGS[@]}" init >>"$LOG" 2>&1 || exit 1
  python3 "$ARTICLE_ROOT/scripts/media_create_once.py" \
    arm --run-dir "$RUN_DIR" >>"$LOG" 2>&1 || {
    echo "=== article-daily media create-once arm failed closed ===" >>"$LOG"
    exit 1
  }
fi

# The foreground model owns the pass until it exits. A provider failure after agent execution
# starts never replays the complete prompt on another provider because public side effects may exist.
MODEL_PASS_PID=""
drain_generation_workers() {
  local worker_pid
  for worker_pid in "${MODEL_PASS_PID:-}" "${JUDGE_BROKER_PID:-}"; do
    [ -n "$worker_pid" ] || continue
    kill -TERM "$worker_pid" 2>/dev/null || true
    wait "$worker_pid" 2>/dev/null || true
  done
  MODEL_PASS_PID=""
  JUDGE_BROKER_PID=""
}
run_model_pass() {
  local active_prompt_file="${1:-$PROMPT_FILE}" rc
  BOUNDED_EXEC_STOP_PATHS="${LIFE_MANAGER_HOST_STATE_DIR:-$HOME/.local/state/life-manager/state}/disk-writers.stop" \
  ARTICLE_RUN_ID="$RUN_TS" ARTICLE_MODEL_LOG="$LOG" \
    python3 "$ARTICLE_ROOT/runtime/bounded-exec.py" \
      "$ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS" \
      "$ARTICLE_MODEL_RUNNER" agent --prompt-file "$active_prompt_file" &
  MODEL_PASS_PID=$!
  wait "$MODEL_PASS_PID"
  rc=$?
  MODEL_PASS_PID=""
  return "$rc"
}

# AUTH FAILURE SAFETY: a previous wrapper retried this same full prompt after 30 seconds.
# Once autopublish is armed, an auth-looking final error can follow real partial side effects;
# replaying the whole prompt can duplicate already-live posts. The separate 300-second worker
# owns platform-scoped, evidence-backed recovery; this foreground call always runs once.
AUTH_FAIL_PATTERN='Failed to authenticate|OAuth session expired|could not be refreshed'
ALERT_SENT=0
LOG_OFFSET=$(wc -c <"$LOG" 2>/dev/null || echo 0)
GENERATION_ATTEMPT_ACTIVE=1
archive_generation_interruption() {
  local interruption_rc="$1"
  trap - INT TERM
  drain_generation_workers
  if [ "$GENERATION_ATTEMPT_ACTIVE" -eq 1 ]; then
    python3 "$GENERATION_STATE" "${GENERATION_ARGS[@]}" \
      archive-interrupted --return-code "$interruption_rc" >>"$LOG" 2>&1 || \
      echo "=== article-daily interruption archive failed closed rc=$interruption_rc ===" >>"$LOG"
    GENERATION_ATTEMPT_ACTIVE=0
  fi
  exit "$interruption_rc"
}
trap 'archive_generation_interruption 130' INT
trap 'archive_generation_interruption 143' TERM
python3 "$GENERATION_STATE" "${GENERATION_ARGS[@]}" begin \
  --owner-pid "$$" >>"$LOG" 2>&1 || {
  GENERATION_ATTEMPT_ACTIVE=0
  trap - INT TERM
  echo "=== article-daily generation begin BLOCK: state is not pre-publication safe ===" >>"$LOG"
  exit 0
}
run_model_pass
RC=$?
drain_generation_workers
if [ "$RC" -eq 124 ] || [ "$RC" -eq 130 ] || [ "$RC" -eq 143 ]; then
  python3 "$GENERATION_STATE" "${GENERATION_ARGS[@]}" \
    archive-interrupted --return-code "$RC" >>"$LOG" 2>&1 || \
    echo "=== article-daily interruption archive failed closed rc=$RC ===" >>"$LOG"
else
  python3 "$GENERATION_STATE" "${GENERATION_ARGS[@]}" result --return-code "$RC" >>"$LOG" 2>&1 || {
    echo "=== article-daily generation result classification failed closed ===" >>"$LOG"
  }
fi
GENERATION_ATTEMPT_ACTIVE=0
trap - INT TERM
PASS_OUTPUT="$(tail -c +$((LOG_OFFSET + 1)) "$LOG" 2>/dev/null)"
if [ "$RC" -ne 0 ] && printf '%s' "$PASS_OUTPUT" | grep -qE "$AUTH_FAIL_PATTERN"; then
  echo "=== article-daily AUTH FAILURE detected rc=$RC; replay requires a complete current-run resume plan $(date '+%F %T %Z') ===" >>"$LOG"
  if command -v telegram_notify >/dev/null 2>&1; then
    telegram_notify "認証の確認で停止しました。記事の公開処理はまだ完了していません。同じ記事の保存済み状態が再開可能になったら、自動的に再試行します。" >>"$LOG" 2>&1 \
      || echo "=== article-daily: auth Telegram notify failed $(date '+%F %T %Z') ===" >>"$LOG"
    ALERT_SENT=1
  fi
fi
# self-heal L2 (spec #22, general catch-all): the block above only alerts for the ONE known
# auth-failure signature. Any OTHER non-zero exit (a different crash, a different upstream
# error) previously fell straight through to `exit 0` below with nothing sent -- the exact
# silent-failure class this task exists to close. Fires once, only if the auth-specific path
# above did not already send its own alert for this same RC.
if [ "$RC" -ne 0 ] && [ "$ALERT_SENT" -ne 1 ]; then
  # Bounded alert body: archive manifests are single multi-KB JSON lines and
  # were bombing Dais's Telegram (2026-07-25). Drop them, trim each line, cap
  # the whole message.
  TAIL_LOG="$(tail -40 "$LOG" 2>/dev/null | grep -v '"archive_manifest"' | cut -c1-200 | tail -20 | head -c 1200)"
  if command -v telegram_notify >/dev/null 2>&1; then
    telegram_notify "記事の公開処理が途中で停止しました。公開済みとは数えず、保存済みの状態を次の自動処理で再確認します。" >>"$LOG" 2>&1 \
      || echo "=== article-daily: fallback Telegram notify failed $(date '+%F %T %Z') ===" >>"$LOG"
    ALERT_SENT=1
  fi
fi

# LEDGER COMPLETION CHECK: rc=0 is not completion evidence. The daily creator runs the
# side-effecting agent once; every later same-run attempt belongs to the 300-second pending
# worker for the same immutable active-four run.
pass_is_complete() {
  # Completion is scoped to this immutable run ID. A same-day row, quality carry-over, or one
  # successful platform from another run can never suppress retry of this run.
  python3 "$ARTICLE_ROOT/scripts/article-run-complete.py" \
    --ledger "$LEDGER" --run-id "$RUN_TS" --armed "$AUTOPUBLISH" \
    --publication-state "$RUN_DIR/gates/publication-state.json"
}

if ! pass_is_complete; then
  QUALITY_ACTION="$(jq -r '.action // empty' "$RUN_DIR/gates/quality-self-heal.json" 2>/dev/null || true)"
  if [ "$QUALITY_ACTION" = "block_freeze" ] && [ ! -f "$RUN_DIR/gates/publication-state.json" ]; then
    echo "=== article-daily: quality-blocked with zero publication state; bounded quality-feedback recovery owns same run=$RUN_TS $(date '+%F %T %Z') ===" >>"$LOG"
    if [ "$ALERT_SENT" -ne 1 ] && command -v telegram_notify >/dev/null 2>&1; then
      telegram_notify "Writer品質回復へ引継ぎ: run $RUN_TS は公開前の品質ゲートで停止し、現在のpublication stateと公開URLは0件です。同一runのbounded quality-feedback recoveryが、一次証拠を追加した1回限定の再評価を担当します。receipt=$RUN_DIR/gates/quality-self-heal.json" >>"$LOG" 2>&1 \
        || echo "=== article-daily: quality-block Telegram notify failed $(date '+%F %T %Z') ===" >>"$LOG"
      ALERT_SENT=1
    fi
  elif [ -f "$RUN_DIR/gates/publication-state.json" ]; then
    echo "=== article-daily: active-four incomplete; durable pending worker owns same run=$RUN_TS without full-prompt replay $(date '+%F %T %Z') ===" >>"$LOG"
    if [ "$ALERT_SENT" -ne 1 ] && command -v telegram_notify >/dev/null 2>&1; then
      telegram_notify "記事はまだ公開完了ではありません。未確認の公開先を、同じ記事のまま5分ごとの自動処理で再確認します。" >>"$LOG" 2>&1 \
        || echo "=== article-daily: incomplete-run Telegram notify failed $(date '+%F %T %Z') ===" >>"$LOG"
      ALERT_SENT=1
    fi
  else
    echo "=== article-daily: generation incomplete before publication state; same-run generation reconciler owns run=$RUN_TS $(date '+%F %T %Z') ===" >>"$LOG"
    if [ "$ALERT_SENT" -ne 1 ] && command -v telegram_notify >/dev/null 2>&1; then
      telegram_notify "記事の準備が公開前に停止しました。公開URLはまだ確認できていません。保存済みの準備状態を自動処理が再確認します。" >>"$LOG" 2>&1 \
        || echo "=== article-daily: generation-incomplete Telegram notify failed $(date '+%F %T %Z') ===" >>"$LOG"
      ALERT_SENT=1
    fi
  fi
fi

# RUN RECORD wrap-up (spec 47): capture this pass raw stdout mechanically (does not depend on the
# agent having followed STEP 0.6 -- $PASS_OUTPUT is this run own slice of $LOG regardless), then
# prune state/runs/ down to the newest 30 generations. Daily run IDs sort chronologically.
printf '%s\n' "$PASS_OUTPUT" > "$RUN_DIR/model-stdout.log" 2>/dev/null || true
python3 "$ARTICLE_ROOT/scripts/prune-article-runs.py" \
  --runs-root "$RUNS_ROOT" --keep 30 >>"$LOG" 2>&1 || \
  echo "=== article-daily: run pruning failed; retained all generations $(date '+%F %T %Z') ===" >>"$LOG"
echo "=== article-daily done rc=$RC $(date '+%F %T %Z') ===" >>"$LOG"
if pass_is_complete; then
  python3 "$ARTICLE_ROOT/scripts/article-completion-notify.py" \
    --state "$RUN_DIR/gates/publication-state.json" --ledger "$LEDGER" \
    --target "$TELEGRAM_TARGET_ID" >>"$LOG" 2>&1 || \
    echo "=== article-daily: active-four completion notification remains pending $(date '+%F %T %Z') ===" >>"$LOG"
  touch "$STATE_DIR/.article-loop-last-pass" 2>/dev/null || true
fi
exit "$RC"
