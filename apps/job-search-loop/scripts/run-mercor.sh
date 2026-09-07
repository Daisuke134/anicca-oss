#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "$SCRIPT_DIR/runtime-paths.sh"

RUN_ID="mercor-$(date +%Y%m%d-%H%M%S)-$$"
MERCOR_STATE_ROOT="${MERCOR_APPLICATION_STATE_ROOT:-${JOB_SEARCH_STATE_ROOT}/mercor}"
EVIDENCE="$JOB_SEARCH_STATE_ROOT/evidence/$RUN_ID"
CDP_URL="${MERCOR_CDP_BASE_URL:-http://127.0.0.1:9334}"
CDP_PAGE_WS="${MERCOR_CDP_PAGE_WS:-}"
MERCOR_PROFILE="${MERCOR_PROFILE:-$JOB_SEARCH_PROFILE}"
if [[ -z "${MERCOR_RESUME:-}" && -f "$MERCOR_STATE_ROOT/resume-state.json" ]]; then
  MERCOR_RESUME=$(
    "$JOB_SEARCH_JQ" -er '.resume_file | select(type == "string" and length > 0)' \
      "$MERCOR_STATE_ROOT/resume-state.json"
  )
fi
MERCOR_RESUME="${MERCOR_RESUME:-${XDG_DATA_HOME:-$HOME/.local/share}/anicca/job-search/mercor-resume.pdf}"
RESULT="$EVIDENCE/agent/mercor-pass-summary.json"
PASS_STDERR="$EVIDENCE/mercor-pass.stderr.log"
TERMINAL="$EVIDENCE/mercor-pass-terminal.json"
REPORT="$EVIDENCE/telegram-report.json"
FINAL_REASON="mercor_result_missing"

mkdir -p "$MERCOR_STATE_ROOT" "$JOB_SEARCH_STATE_ROOT/evidence" "$JOB_SEARCH_STATE_ROOT/logs" "$EVIDENCE"
chmod 700 "$JOB_SEARCH_STATE_ROOT" "$MERCOR_STATE_ROOT" "$JOB_SEARCH_STATE_ROOT/evidence" "$JOB_SEARCH_STATE_ROOT/logs" "$EVIDENCE"
export LIFE_MANAGER_PROVIDER_LEASE_PATH="$MERCOR_STATE_ROOT/.pass.lock"
export PYTHONPATH="$JOB_SEARCH_APP_ROOT"

finalize() {
  local original_rc="$1"
  trap - EXIT
  set +e
  "$JOB_SEARCH_PYTHON" -m job_search_loop.mercor_reporting \
    --terminal-report \
    --run-id "$RUN_ID" \
    --result "$RESULT" \
    --reason "$FINAL_REASON" \
    --outbox "$JOB_SEARCH_STATE_ROOT/telegram-outbox.sqlite3" \
    --gate-store "$MERCOR_STATE_ROOT/human-gates.jsonl" \
    --terminal "$TERMINAL" \
    --output "$REPORT"
  if [[ ! -f "$TERMINAL" ]]; then
    printf '{"status":"failed","inspected_listings":[],"submitted":[],"needs_human":[],"blocked":["mercor_reporting_failed"],"evidence":{"page_url":"","screenshot_path":"","dom_path":""}}\n' >"$TERMINAL"
    chmod 600 "$TERMINAL"
  fi
  if [[ ! -f "$REPORT" ]]; then
    printf '{"delivery":"delivery_unknown","event_key":"mercor-pass:%s","reason":"mercor_reporting_failed"}\n' "$RUN_ID" >"$REPORT"
    chmod 600 "$REPORT"
  fi
  find "$EVIDENCE" -type d -exec chmod 700 {} +
  find "$EVIDENCE" -type f -exec chmod 600 {} +
  exit "$original_rc"
}
trap 'finalize $?' EXIT

PASS_PROMPT="$EVIDENCE/mercor-pass.md"
PASS_SCHEMA="$EVIDENCE/mercor-pass-result.v1.schema.json"
cp "$JOB_SEARCH_APP_ROOT/prompts/mercor-pass.md" "$PASS_PROMPT"
cp "$JOB_SEARCH_APP_ROOT/schemas/mercor-pass-result.v1.schema.json" "$PASS_SCHEMA"
chmod 600 "$PASS_PROMPT" "$PASS_SCHEMA"

set +e
"$JOB_SEARCH_PYTHON" -m job_search_loop.browser_owner \
  --endpoint "$CDP_URL" \
  --output "$EVIDENCE/browser-owner.json"
BROWSER_RC=$?
set -e
if [[ "$BROWSER_RC" -ne 0 ]]; then
  FINAL_REASON="browser_owner_failed"
  exit "$BROWSER_RC"
fi

MERCOR_PASS_ARGS=(
  --state-root "$MERCOR_STATE_ROOT"
  --profile "$MERCOR_PROFILE"
  --resume "$MERCOR_RESUME"
  --cdp-url "$CDP_URL"
  --prompt "$PASS_PROMPT"
  --schema "$PASS_SCHEMA"
  --evidence-dir "$EVIDENCE/agent"
  --workdir "$JOB_SEARCH_REPO_ROOT"
  --run-id "$RUN_ID"
)
if [[ -n "$CDP_PAGE_WS" ]]; then
  MERCOR_PASS_ARGS+=(--cdp-page-ws "$CDP_PAGE_WS")
fi
set +e
"$JOB_SEARCH_PYTHON" -m job_search_loop.mercor_pass \
  "${MERCOR_PASS_ARGS[@]}" 2>"$PASS_STDERR"
PASS_RC=$?
set -e
if [[ "$PASS_RC" -eq 75 ]] && grep -Fqx "LIFE_MANAGER_PROVIDER_LEASE_BUSY" "$PASS_STDERR"; then
  FINAL_REASON="mercor_pass_already_running"
  exit 0
fi
if [[ "$PASS_RC" -ne 0 ]]; then
  FINAL_REASON="mercor_runner_failed"
  exit "$PASS_RC"
fi

set +e
"$JOB_SEARCH_PYTHON" -c 'import json, sys; value=json.load(open(sys.argv[1])); raise SystemExit(0 if isinstance(value, dict) and isinstance(value.get("status"), str) else 2)' "$RESULT"
RESULT_RC=$?
set -e
if [[ "$RESULT_RC" -ne 0 ]]; then
  FINAL_REASON="mercor_result_invalid"
  exit "$RESULT_RC"
fi

EARNINGS_EVIDENCE="$EVIDENCE/earnings"
EARNINGS_SNAPSHOT="$MERCOR_STATE_ROOT/earnings-readback.json"
mkdir -p "$EARNINGS_EVIDENCE"
chmod 700 "$EARNINGS_EVIDENCE"
set +e
"$JOB_SEARCH_PYTHON" -m job_search_loop.mercor_earnings_capture \
  --cdp "$CDP_URL" \
  --evidence-dir "$EARNINGS_EVIDENCE" \
  --output "$EARNINGS_SNAPSHOT"
CAPTURE_RC=$?
set -e
if [[ "$CAPTURE_RC" -ne 0 ]]; then
  FINAL_REASON="earnings_capture_failed"
  exit "$CAPTURE_RC"
fi

set +e
"$JOB_SEARCH_PYTHON" -m job_search_loop.mercor_earnings_sync \
  --snapshot "$EARNINGS_SNAPSHOT" \
  --store "$MERCOR_STATE_ROOT/work-events.jsonl" \
  --outbox "$JOB_SEARCH_STATE_ROOT/telegram-outbox.sqlite3" \
  --output "$EVIDENCE/mercor-earnings-sync.json"
SYNC_RC=$?
set -e
if [[ "$SYNC_RC" -ne 0 ]]; then
  FINAL_REASON="earnings_sync_failed"
  exit "$SYNC_RC"
fi

FINAL_REASON="success"
exit 0
