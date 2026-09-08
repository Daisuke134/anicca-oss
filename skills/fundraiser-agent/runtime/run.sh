#!/bin/bash
set -uo pipefail

REPO_ROOT="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$REPO_ROOT" ] || { echo "fundraiser: repository unavailable" >&2; exit 2; }
STATE_ROOT="${FUNDRAISER_STATE_ROOT:-$HOME/.local/state/life-manager/fundraiser}"
LOCK_DIR="$STATE_ROOT/run.lock"
LOCK_HELPER="$REPO_ROOT/skills/fundraiser-agent/runtime/run-lock.sh"
LOG="$STATE_ROOT/fundraiser.log"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
EVIDENCE_DIR="$STATE_ROOT/evidence/$RUN_ID"
RUN_AGENT="$REPO_ROOT/skills/earn/marketing-engine/run_agent.sh"
PROMPT="$REPO_ROOT/skills/fundraiser-agent/prompts/daily.md"
SCHEMA="$REPO_ROOT/skills/fundraiser-agent/runtime/pass-result.schema.json"
SENDER="$REPO_ROOT/skills/_shared/send-telegram.sh"
PHOTO_SENDER="$REPO_ROOT/skills/_shared/send-telegram-photo.sh"
LOOP_CLI="${LIFE_MANAGER_LOOP_CLI:-$REPO_ROOT/bin/lm-loop}"
MIN_FREE_KIB=$((1536 * 1024))
PRESSURE_FREE_KIB=$((2 * 1024 * 1024))

available_kib() {
  df -Pk "$STATE_ROOT" 2>/dev/null | awk 'NR==2 {print $4}'
}

cdp_healthy() {
  curl -fsS --max-time 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1
}

# An application browser pass temporarily needs close to 1 GiB. Starting below this floor
# repeatedly ended with ENOSPC before the runner could persist its summary or proof.
# Keep launchd enabled, ask the existing disk owner to reclaim only classified
# regenerable artifacts, and let the next scheduled wake retry naturally.
FREE_KIB="$(available_kib)"
if [ -z "$FREE_KIB" ] || ! [[ "$FREE_KIB" =~ ^[0-9]+$ ]]; then
  echo "fundraiser: disk preflight unavailable" >&2
  exit 2
fi
if [ "$FREE_KIB" -lt "$PRESSURE_FREE_KIB" ]; then
  "$LOOP_CLI" restart life-manager-disk-cleanup >/dev/null 2>&1 || true
  echo "fundraiser: deferred disk policy available_kib=$FREE_KIB required_kib=$PRESSURE_FREE_KIB" >>"$LOG"
  exit 75
fi

if ! cdp_healthy; then
  "$LOOP_CLI" restart life-manager-daily-driver >/dev/null 2>&1 || true
  CDP_READY=false
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    if cdp_healthy; then
      CDP_READY=true
      break
    fi
    sleep 2
  done
  if [ "$CDP_READY" != true ]; then
    echo "fundraiser: deferred cdp endpoint unavailable after owner recovery" >>"$LOG"
    exit 75
  fi
fi

mkdir -p "$STATE_ROOT/evidence" "$EVIDENCE_DIR"
chmod 700 "$STATE_ROOT" "$STATE_ROOT/evidence" "$EVIDENCE_DIR"
source "$LOCK_HELPER"
if ! acquire_run_lock "$LOCK_DIR"; then
  echo "fundraiser: prior pass still owns the loop" >>"$LOG"
  exit 0
fi
trap 'release_run_lock "$LOCK_DIR"' EXIT

export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LIFE_MANAGER_REPO="$REPO_ROOT"
export FUNDRAISER_RUN_ID="$RUN_ID"
export FUNDRAISER_STATE_ROOT="$STATE_ROOT"
export FUNDRAISER_EVIDENCE_DIR="$EVIDENCE_DIR"
export FUNDRAISER_RECEIPTS="$STATE_ROOT/application-receipts.jsonl"
export FUNDRAISER_APPLICATIONS_DIR="$STATE_ROOT/applications"
export FUNDRAISER_RECORD_APPLICATION="$REPO_ROOT/skills/fundraiser-agent/runtime/record-application.py"
export FUNDRAISER_CURSOR="$STATE_ROOT/cursor.json"
export FUNDRAISER_CDP_ENDPOINT="http://127.0.0.1:9222"
export FUNDRAISER_X_CDP_ENDPOINT="http://127.0.0.1:9222"
export FUNDRAISER_TELEGRAM_SENDER="$SENDER"
export FUNDRAISER_TELEGRAM_PHOTO_SENDER="$PHOTO_SENDER"
export FUNDRAISER_CAPTCHA_MODE="existing-capsolver-only"

CONTEXT_META="$(node --input-type=module - "$REPO_ROOT" <<'NODE'
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const root = process.argv[2];
const context = JSON.parse(await readFile(`${root}/.agents/startup-context.json`, "utf8"));
const { contextDigest } = await import(pathToFileURL(`${root}/scripts/startup-context/lib.mjs`));
process.stdout.write(`${context.context_version}\n${contextDigest(context)}\n`);
NODE
)" || { echo "fundraiser: canonical context preflight failed" >>"$LOG"; exit 2; }
export FUNDRAISER_CONTEXT_VERSION="$(printf '%s\n' "$CONTEXT_META" | sed -n '1p')"
export FUNDRAISER_CONTEXT_DIGEST="$(printf '%s\n' "$CONTEXT_META" | sed -n '2p')"
[ -n "$FUNDRAISER_CONTEXT_VERSION" ] && [ -n "$FUNDRAISER_CONTEXT_DIGEST" ] || {
  echo "fundraiser: canonical context metadata unavailable" >>"$LOG"
  exit 2
}

RUNTIME_PROMPT="$EVIDENCE_DIR/runtime-prompt.md"
{
  cat "$PROMPT"
  cat <<EOF

## Concrete local runtime

- This is real run \`$RUN_ID\`, owned by \`ai.anicca.fundraiser\`.
- Work in \`$REPO_ROOT\`; use the existing authenticated Chrome CDP endpoint \`http://127.0.0.1:9222\`.
- Search both the live Web and rendered authenticated X UI. X is discovery only; verify on the official program website before applying.
- Use existing browser helpers under \`skills/browser/\`; do not launch or kill a browser.
- If \`127.0.0.1:9222\` becomes connection-refused during this pass, do not record a candidate failure yet. Execute \`$LOOP_CLI restart life-manager-daily-driver\`, wait up to 20 seconds for \`curl -fsS --max-time 2 http://127.0.0.1:9222/json/version\` to succeed, reacquire a fresh fundraiser lease, and retry the same candidate observation once. Only checkpoint the transport if that exact managed recovery fails. Never launch or kill Chromium directly.
- Read private founder values only from \`~/.config/anicca/job-search/profile.json\` and \`~/.local/share/anicca/credentials.json\`; never print or report their values.
- Never append a \`submitted_verified\` row directly. Before Submit, create a mode-600 draft JSON containing organization, program, cohort_window, account, official_url, contact {method,destination}, every rendered question and actual answer in question_answers, attachment names, the exact non-secret claims/source paths used in context_used, context_version \`$FUNDRAISER_CONTEXT_VERSION\`, and context_digest \`$FUNDRAISER_CONTEXT_DIGEST\`. Run \`python3 "$REPO_ROOT/skills/fundraiser-agent/runtime/record-application.py" --prepare --draft <draft> --ledger "$STATE_ROOT/application-receipts.jsonl" --applications-dir "$STATE_ROOT/applications" --expected-context-version "$FUNDRAISER_CONTEXT_VERSION" --expected-context-digest "$FUNDRAISER_CONTEXT_DIGEST"\` and require its prepared application_digest before claiming the final effect. This pre-submit gate rejects prior terminal applications even when cohort dates or URL spelling drift. After official screenshot and Telegram photo delivery, add submitted_at and evidence {completion_png,telegram_photo_message_id,provider_readback} without changing the prepared fields; then run \`python3 "$REPO_ROOT/skills/fundraiser-agent/runtime/record-application.py" --draft <draft> --ledger "$STATE_ROOT/application-receipts.jsonl" --applications-dir "$STATE_ROOT/applications" --run-id "$RUN_ID" --expected-context-version "$FUNDRAISER_CONTEXT_VERSION" --expected-context-digest "$FUNDRAISER_CONTEXT_DIGEST"\`. Only its successful output establishes \`submitted_verified\`. Use direct compact rows only for non-success terminal states.
- Write the durable next discovery cursor atomically to \`$STATE_ROOT/cursor.json\`.
- Immediately after every candidate terminal, execute \`bash $SENDER "Codex::: Fundraiser: <program, truthful status, non-secret readback, running counts>"\` and require \`TELEGRAM_SENT=true\`.
- An application is verified only after its official form completion page or exact Gmail Sent message is captured as a PNG, visually readable, sent with \`bash $PHOTO_SENDER "<png>" "Codex::: Fundraiser proof: <program>"\`, and the output contains \`TELEGRAM_PHOTO_SENT=true MSGID=<id>\`. Save them as exact top-level receipt keys \`"completion_png":"<absolute path>"\` and \`"telegram_photo_message_id":<integer>\`; mentioning them only inside \`readback_reference\` is invalid.
- First click a visible ordinary reCAPTCHA checkbox once through the rendered UI and observe. If it produces an image/audio challenge, use only the already-installed CapSolver/tier-a-bypass route found locally. Never weaken, evade, or disable provider security. If unavailable, checkpoint that candidate and continue to the next one.
- Spend this pass applying, not editing product code. Continue after the first submission. Return status=failure when submitted=0.
EOF
} >"$RUNTIME_PROMPT"
chmod 600 "$RUNTIME_PROMPT"

echo "=== fundraiser $RUN_ID start ===" >>"$LOG"
set +e
cat "$RUNTIME_PROMPT" | "$RUN_AGENT" \
  --task-class application-lane-agent \
  --schema "$SCHEMA" \
  --evidence-dir "$EVIDENCE_DIR" \
  --task-label fundraiser-continuous \
  --loop fundraiser \
  --workdir "$REPO_ROOT" >>"$LOG" 2>&1
RC=$?
set -e

SUMMARY_STATUS="runner_failure"
COUNTS="submitted=0 unknown=0 checkpoints=0"
if [ "$RC" -eq 0 ] && [ -f "$EVIDENCE_DIR/summary.json" ]; then
  READBACK="$(python3 - "$EVIDENCE_DIR/summary.json" <<'PY'
import json, pathlib, sys
summary = json.loads(pathlib.Path(sys.argv[1]).read_text())
result = json.loads(pathlib.Path(summary["result_path"]).read_text())
print(result["status"])
print(f'submitted={result["submitted"]} unknown={result["submit_unknown"]} checkpoints={result["checkpoints"]}')
PY
)" || RC=1
  SUMMARY_STATUS="$(printf '%s\n' "$READBACK" | sed -n '1p')"
  COUNTS="$(printf '%s\n' "$READBACK" | sed -n '2p')"
fi

if [ -f "$FUNDRAISER_RECEIPTS" ]; then
  LEDGER_COUNTS="$(python3 - "$FUNDRAISER_RECEIPTS" "$RUN_ID" <<'PY'
import json, pathlib, sys

submitted = unknown = checkpoints = 0
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    try:
        receipt = json.loads(line)
    except json.JSONDecodeError:
        continue
    if receipt.get("run_id") != sys.argv[2]:
        continue
    status = receipt.get("status")
    submitted += status == "submitted_verified"
    unknown += status == "submit_unknown"
    checkpoints += status == "human_checkpoint"
print(f"submitted={submitted} unknown={unknown} checkpoints={checkpoints}")
PY
)" || true
  [ -n "$LEDGER_COUNTS" ] && COUNTS="$LEDGER_COUNTS"
fi

REPORT="Codex::: Fundraiser wake $RUN_ID finished: status=$SUMMARY_STATUS, $COUNTS. Evidence: $EVIDENCE_DIR"
"$SENDER" "$REPORT" >>"$LOG" 2>&1 || RC=1
echo "=== fundraiser $RUN_ID end rc=$RC status=$SUMMARY_STATUS $COUNTS ===" >>"$LOG"
exit "$RC"
