#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/runtime-paths.sh"
export CLOAK_LEASE_HOLDER_PID=$$

JOB_SEARCH_DISK_GUARD="${JOB_SEARCH_DISK_GUARD:-$JOB_SEARCH_REPO_ROOT/runtime/host/disk_admission.py}"
if [[ ! -f "$JOB_SEARCH_DISK_GUARD" || -L "$JOB_SEARCH_DISK_GUARD" || ! -r "$JOB_SEARCH_DISK_GUARD" ]]; then
  print -u2 "job-search daily: disk guard is missing or unsafe"
  exit 75
fi
unset DISK_CONTROL_STATE_DIR OPENCLAW_STATE_DIR \
  GIG_DISK_HEADROOM_KIB GIG_HOST_STATE_DIR GIG_STATE_DIR \
  GIG_IGNORE_DISK_PRESSURE_BLOCK GIG_IGNORE_DISK_WRITERS_STOP
LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK=1
LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP=1
LIFE_MANAGER_DISK_HEADROOM_KIB=524288
LIFE_MANAGER_HOST_STATE_DIR="$HOME/.local/state/life-manager/state"
LIFE_MANAGER_PRODUCER_STATE_DIR="$HOME/.local/state/life-manager/job-search-daily"
export LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP \
  LIFE_MANAGER_DISK_HEADROOM_KIB LIFE_MANAGER_HOST_STATE_DIR LIFE_MANAGER_PRODUCER_STATE_DIR
if ! /usr/bin/python3 -I "$JOB_SEARCH_DISK_GUARD" /usr/bin/true; then
  print -u2 "job-search daily: disk guard blocked model wake"
  exit 75
fi

RUN_ID="daily-$(date +%Y%m%d-%H%M%S)"
EVIDENCE="$JOB_SEARCH_STATE_ROOT/evidence/$RUN_ID"
TELEGRAM_OUTBOX="$JOB_SEARCH_STATE_ROOT/telegram-outbox.sqlite3"
BROWSER_STATE="$JOB_SEARCH_STATE_ROOT/browser-agent"
BROWSER_SCRATCH="$EVIDENCE/scratch"

mkdir -p "$EVIDENCE" "$JOB_SEARCH_STATE_ROOT/logs" "$BROWSER_STATE" "$BROWSER_SCRATCH"
chmod 700 \
  "$JOB_SEARCH_STATE_ROOT" \
  "$JOB_SEARCH_STATE_ROOT/evidence" \
  "$EVIDENCE" \
  "$BROWSER_STATE" \
  "$BROWSER_SCRATCH" \
  "$JOB_SEARCH_STATE_ROOT/logs"
export PYTHONPATH="$JOB_SEARCH_APP_ROOT"
export JOB_SEARCH_BROWSER_OWNER_EVIDENCE="$EVIDENCE/browser-owner.json"
export JOB_SEARCH_BROWSER_STATE_ROOT="$BROWSER_STATE"
export JOB_SEARCH_BROWSER_SCRATCH="$BROWSER_SCRATCH"
"$JOB_SEARCH_PYTHON" -m job_search_loop.application_reporting deliver \
  --ledger "$JOB_SEARCH_STATE_ROOT/ledger.sqlite3" \
  --outbox "$TELEGRAM_OUTBOX" \
  --media-root "$JOB_SEARCH_TELEGRAM_MEDIA" \
  --output "$EVIDENCE/resume-deliver-before.json"
JAPAN_DAY=$(TZ=Asia/Tokyo /bin/date +%F)
WORKDAY_DISCOVERY_RESULT="$EVIDENCE/workday-discovery.json"
WORKDAY_SOURCES="$EVIDENCE/workday-sources.json"
PERSISTED_WORKDAY_SOURCES="$JOB_SEARCH_STATE_ROOT/workday-sources.v1.json"
WORKDAY_SOURCE_MAINTAINED_AT="$JOB_SEARCH_STATE_ROOT/workday-sources-maintained-at"
WORKDAY_SNAPSHOT="$EVIDENCE/workday-job-snapshot.json"
PERSISTED_WORKDAY_SNAPSHOT="$JOB_SEARCH_STATE_ROOT/workday-job-snapshot.v1.json"
RUNNER_SUMMARY="$EVIDENCE/summary.json"
refresh_summary() {
  "$JOB_SEARCH_PYTHON" -m job_search_loop.summary \
    --ledger "$JOB_SEARCH_STATE_ROOT/ledger.sqlite3" \
    --output "$JOB_SEARCH_STATE_ROOT/summary.v2.json" \
    --compat-output "$JOB_SEARCH_STATE_ROOT/summary.v1.json" \
    --day "$JAPAN_DAY" \
    --model-route "shared-agent-runner"
}
report_wake() {
  local original_rc=$?
  local report_rc
  trap - EXIT
  set +e
  "$JOB_SEARCH_PYTHON" -m job_search_loop.application_reporting wake \
    --ledger "$JOB_SEARCH_STATE_ROOT/ledger.sqlite3" \
    --outbox "$TELEGRAM_OUTBOX" \
    --output "$EVIDENCE/wake-report.json" \
    --run-id "$RUN_ID" \
    --day "$JAPAN_DAY" \
    --runner-summary "$RUNNER_SUMMARY" \
    --discovery "$WORKDAY_DISCOVERY_RESULT"
  report_rc=$?
  set -e
  if [[ "$original_rc" -eq 0 && "$report_rc" -ne 0 ]]; then
    exit "$report_rc"
  fi
  exit "$original_rc"
}
trap report_wake EXIT
"$JOB_SEARCH_PYTHON" -m job_search_loop.browser_owner \
  --endpoint "http://127.0.0.1:9222" \
  --output "$JOB_SEARCH_BROWSER_OWNER_EVIDENCE"
CANDIDATE_MEMORY="$JOB_SEARCH_STATE_ROOT/candidate-memory.v1.json"
MATERIALS_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/anicca/job-search/materials"
RESUME_PATHS=$(
  "$JOB_SEARCH_PYTHON" -m job_search_loop.resume_routing \
    --materials-root "$MATERIALS_ROOT" \
    --list-resumes
)
RESUME_ARGUMENTS=()
while IFS= read -r resume_path; do
  RESUME_ARGUMENTS+=(--resume "$resume_path")
done < <(print -r -- "$RESUME_PATHS" | "$JOB_SEARCH_JQ" -r '.[]')
"$JOB_SEARCH_PYTHON" -m job_search_loop.browser_agent.candidate_memory \
  --profile "$JOB_SEARCH_PROFILE" \
  "${RESUME_ARGUMENTS[@]}" \
  --output "$CANDIDATE_MEMORY" >"$EVIDENCE/candidate-memory-receipt.json"
chmod 600 "$EVIDENCE/candidate-memory-receipt.json"
export JOB_SEARCH_CANDIDATE_MEMORY="$CANDIDATE_MEMORY"
export JOB_SEARCH_ANSWER_MEMORY="$JOB_SEARCH_STATE_ROOT/answer-memory.v1.json"
export JOB_SEARCH_MACHINE_CREDENTIALS="${XDG_DATA_HOME:-$HOME/.local/share}/anicca/credentials.json"
NOW_EPOCH=$(date +%s)
LAST_SOURCE_MAINTENANCE=0
if [[ -f "$WORKDAY_SOURCE_MAINTAINED_AT" ]]; then
  LAST_SOURCE_MAINTENANCE=$(<"$WORKDAY_SOURCE_MAINTAINED_AT")
fi
if [[ ! -f "$PERSISTED_WORKDAY_SOURCES" ]] || \
   (( NOW_EPOCH - LAST_SOURCE_MAINTENANCE >= 86400 )); then
  DISCOVERED_WORKDAY_SOURCES="$EVIDENCE/workday-source-maintenance.json"
  set +e
  "$JOB_SEARCH_PYTHON" -m job_search_loop.workday_source_discovery \
    --candidate-memory "$CANDIDATE_MEMORY" \
    --runner "$JOB_SEARCH_RUNNER" \
    --schema "$JOB_SEARCH_APP_ROOT/schemas/workday-sources.v1.schema.json" \
    --workdir "$JOB_SEARCH_REPO_ROOT" \
    --evidence-root "$EVIDENCE/source-discovery" \
    --previous "$PERSISTED_WORKDAY_SOURCES" \
    --output "$DISCOVERED_WORKDAY_SOURCES"
  WORKDAY_SOURCE_RC=$?
  set -e
  if [[ "$WORKDAY_SOURCE_RC" -eq 0 ]]; then
    mv "$DISCOVERED_WORKDAY_SOURCES" "$PERSISTED_WORKDAY_SOURCES"
    chmod 600 "$PERSISTED_WORKDAY_SOURCES"
    print -r -- "$NOW_EPOCH" >"$WORKDAY_SOURCE_MAINTAINED_AT"
    chmod 600 "$WORKDAY_SOURCE_MAINTAINED_AT"
  else
    printf '%s\n' "Workday registry bootstrap failed closed" >&2
  fi
fi
if [[ -f "$PERSISTED_WORKDAY_SOURCES" ]]; then
  cp "$PERSISTED_WORKDAY_SOURCES" "$WORKDAY_SOURCES"
fi
set +e
if [[ -f "$WORKDAY_SOURCES" ]]; then
"$JOB_SEARCH_PYTHON" -m job_search_loop.workday_search_loop \
  --ledger "$JOB_SEARCH_STATE_ROOT/ledger.sqlite3" \
  --candidate-memory "$CANDIDATE_MEMORY" \
  --sources "$WORKDAY_SOURCES" \
  --runner "$JOB_SEARCH_RUNNER" \
  --schema "$JOB_SEARCH_APP_ROOT/schemas/workday-fit-decision.v1.schema.json" \
  --shortlist-schema "$JOB_SEARCH_APP_ROOT/schemas/workday-shortlist.v1.schema.json" \
  --workdir "$JOB_SEARCH_REPO_ROOT" \
  --evidence-root "$EVIDENCE/qualification" \
  --output "$WORKDAY_DISCOVERY_RESULT" \
  --snapshot "$WORKDAY_SNAPSHOT" \
  --max-candidates 24
WORKDAY_SEARCH_RC=$?
else
  WORKDAY_SEARCH_RC=75
fi
set -e
if [[ -f "$WORKDAY_SNAPSHOT" ]]; then
  cp "$WORKDAY_SNAPSHOT" "$PERSISTED_WORKDAY_SNAPSHOT"
  chmod 600 "$PERSISTED_WORKDAY_SNAPSHOT"
fi
if [[ "$WORKDAY_SEARCH_RC" -ne 0 ]]; then
  printf '%s\n' "Workday search failed closed; no unqualified row can enter the browser lane" >&2
fi
WORKDAY_FAST_PATH_RESULT="$EVIDENCE/workday-fast-path.json"
"$JOB_SEARCH_PYTHON" - "$WORKDAY_FAST_PATH_RESULT" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
path.write_text(
    json.dumps(
        {
            "status": "model_owned",
            "processed": [],
            "excluded": [],
            "owner": "ai.anicca.job-search-daily",
            "reason": "mandatory_browser_lane",
        },
        ensure_ascii=False,
    )
    + "\n",
    encoding="utf-8",
)
os.chmod(path, 0o600)
PY
export JOB_SEARCH_WORKDAY_FAST_PATH_RESULT="$WORKDAY_FAST_PATH_RESULT"
APPLICATION_ID=""
APPLICATION_LIMIT=""
if [[ -f "$WORKDAY_DISCOVERY_RESULT" ]]; then
  APPLICATION_ID=$("$JOB_SEARCH_JQ" -r '.queued_application_ids[0] // empty' "$WORKDAY_DISCOVERY_RESULT")
  if ! APPLICATION_LIMIT=$("$JOB_SEARCH_JQ" -er '
    if (.deficit | type) == "number"
       and (.deficit >= 0)
       and ((.deficit | floor) == .deficit)
    then (.deficit | tostring)
    else error("invalid deficit")
    end
  ' "$WORKDAY_DISCOVERY_RESULT"); then
    print -u2 "job-search daily: malformed rolling deficit; queue closed"
    APPLICATION_LIMIT=0
  fi
else
  print -u2 "job-search daily: missing rolling deficit; queue closed"
  APPLICATION_LIMIT=0
fi
export JOB_SEARCH_APPLICATION_LIMIT="$APPLICATION_LIMIT"
if [[ -n "$APPLICATION_ID" ]]; then
  export JOB_SEARCH_PREFERRED_APPLICATION_ID="$APPLICATION_ID"
  set +e
  "$JOB_SEARCH_PYTHON" -m job_search_loop.application_reporting progress \
    --ledger "$JOB_SEARCH_STATE_ROOT/ledger.sqlite3" \
    --outbox "$TELEGRAM_OUTBOX" \
    --application-id "$APPLICATION_ID" \
    --run-id "$RUN_ID" \
    --output "$EVIDENCE/application-progress.json"
  PROGRESS_RC=$?
  set -e
  if [[ "$PROGRESS_RC" -ne 0 ]]; then
    print -u2 "job-search daily: realtime application progress report failed"
  fi
fi
MODEL_TIMEOUT_SECONDS="${JOB_SEARCH_BROWSER_TIMEOUT_SECONDS:-1800}"
set +e
"$JOB_SEARCH_PYTHON" -m job_search_loop.browser_agent.orchestrator \
  --runner "$JOB_SEARCH_RUNNER" \
  --timeout-seconds "$MODEL_TIMEOUT_SECONDS" \
  --prompt "$JOB_SEARCH_APP_ROOT/prompts/daily-pass.md" \
  --schema "$JOB_SEARCH_APP_ROOT/schemas/pass-result.v1.schema.json" \
  --evidence-dir "$EVIDENCE" \
  --workdir "$JOB_SEARCH_REPO_ROOT" \
  --python "$JOB_SEARCH_PYTHON" \
  --active-provider workday
RUNNER_RC=$?
set -e
set +e
/opt/homebrew/bin/timeout 45 env \
  SESSION_VAULT_PORT=9222 \
  SESSION_VAULT_DIR="$JOB_SEARCH_SESSION_VAULT_DIR" \
  "$JOB_SEARCH_PYTHON" "$JOB_SEARCH_SESSION_VAULT_SCRIPT" dump \
  >"$EVIDENCE/session-vault-dump.json" 2>"$EVIDENCE/session-vault-dump.stderr.log"
VAULT_RC=$?
set -e
chmod 600 "$EVIDENCE/session-vault-dump.json" "$EVIDENCE/session-vault-dump.stderr.log"
if [[ "$VAULT_RC" -ne 0 ]]; then
  printf '%s\n' "job-search session vault snapshot failed; wake evidence preserved" >&2
fi
if [[ "$RUNNER_RC" -ne 0 ]]; then
  refresh_summary
  exit "$RUNNER_RC"
fi
"$JOB_SEARCH_PYTHON" -m job_search_loop.application_reporting deliver \
  --ledger "$JOB_SEARCH_STATE_ROOT/ledger.sqlite3" \
  --outbox "$TELEGRAM_OUTBOX" \
  --media-root "$JOB_SEARCH_TELEGRAM_MEDIA" \
  --output "$EVIDENCE/resume-deliver-after.json"
refresh_summary
