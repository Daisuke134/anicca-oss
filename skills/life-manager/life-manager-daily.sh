#!/usr/bin/env bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# One bounded Life Manager marketing pass. The launchd job, account, rotation and posting
# route predate 9b; only the creative renderer and agent runtime contract change here.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
set -uo pipefail
umask 077

if [ "${LM_DAILY_ACTIVE:-0}" = "1" ]; then
  printf 'life-manager-daily: recursive invocation blocked\n' >&2
  exit 73
fi
export LM_DAILY_ACTIVE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON_BIN:-$(command -v python3 2>/dev/null || true)}"
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] || { echo '{"status":"setup_required","missing":"python3"}' >&2; exit 2; }
RUN_AGENT="${RUN_AGENT_BIN:-$LIFE_MANAGER_REPO/skills/earn/marketing-engine/run_agent.sh}"
VIDEO_GENERATOR="${LM_VIDEO_GENERATOR:-$HERE/../video/daily-lm-video/generate.py}"
VIDEO_DISTRIBUTOR="${LM_VIDEO_DISTRIBUTOR:-$HERE/../video/lm-distribution/distribute.py}"
MARKETING_SELF_IMPROVER="${LM_MARKETING_SELF_IMPROVER:-$HERE/../video/lm-self-improve/daily.py}"
# One data root shared with the argless generate.py defaults (LM_DATA_DIR,
# falling back to ~/.local/state/life-manager); ledgers live at
# <data root>/state/lm-video — the single lm-video path convention.
LM_DATA_ROOT="${LM_DATA_DIR:-$HOME/.local/state/life-manager}"
LOG="${LM_DAILY_LOG:-$LM_DATA_ROOT/logs/life-manager-daily.log}"
RUN_LEDGER="${LM_DAILY_RUN_LEDGER:-$LM_DATA_ROOT/state/lm-video/daily-run-ledger.jsonl}"
USAGE_LEDGER="${LM_DAILY_USAGE_LEDGER:-$LM_DATA_ROOT/state/lm-video/agent-usage.jsonl}"
MARKETING_LEDGER="${LM_MARKETING_LEDGER:-$LIFE_MANAGER_REPO/skills/life-manager/state/marketing-actions.jsonl}"
BANK_PATH="$(cd "$HERE/../video/daily-lm-video" && pwd)/creative-bank.jsonl"
ROTATION_STATE="${LM_VIDEO_STATE:-$LM_DATA_ROOT/state/lm-video/daily-render-state.jsonl}"
mkdir -p "$(dirname "$LOG")" "$(dirname "$RUN_LEDGER")" "$(dirname "$USAGE_LEDGER")"
printf '=== life-manager-daily run %s ===\n' "$(date '+%F %T %Z')" >>"$LOG"

if [ "${AGENT_WIRING_PROBE_ONLY:-0}" = "1" ]; then
  printf '{"task_class":"marketing-agent","runner":"%s","generator":"%s"}\n' "$RUN_AGENT" "$VIDEO_GENERATOR"
  exit 0
fi

set +e
GEN_RESULT="$("$VIDEO_GENERATOR" 2>>"$LOG")"
GEN_RC=$?
set -e
if [ "$GEN_RC" -ne 0 ]; then
  printf 'daily-lm-video failed rc=%s; agent not invoked\n' "$GEN_RC" >>"$LOG"
  exit "$GEN_RC"
fi

set +e
FIELDS="$(GEN_RESULT="$GEN_RESULT" "$PYTHON" -c '
import json, os
lines = os.environ["GEN_RESULT"].splitlines()
if len(lines) != 1:
    raise SystemExit(2)
data = json.loads(lines[0])
creative_id = data.get("selected_id")
output = data.get("output")
duration = data.get("duration_seconds")
if not isinstance(creative_id, str) or not creative_id or not isinstance(output, str) or not output:
    raise SystemExit(2)
float(duration)
print("\t".join((creative_id, output, str(duration))))
' 2>>"$LOG")"
PARSE_RC=$?
set -e
if [ "$PARSE_RC" -ne 0 ]; then
  printf 'daily-lm-video returned invalid result rc=%s; agent not invoked\n' "$PARSE_RC" >>"$LOG"
  exit "$PARSE_RC"
fi
IFS=$'\t' read -r LM_DAILY_CREATIVE_ID LM_DAILY_VIDEO LM_DAILY_VIDEO_DURATION <<<"$FIELDS"
export LM_DAILY_CREATIVE_ID LM_DAILY_VIDEO LM_DAILY_VIDEO_DURATION

LM_DAILY_CREATIVE_ALREADY_POSTED="$(LEDGER="$MARKETING_LEDGER" CREATIVE_ID="$LM_DAILY_CREATIVE_ID" CREATIVE_OUTPUT="$LM_DAILY_VIDEO" "$PYTHON" -c '
import json, os
found = False
try:
    for line in open(os.environ["LEDGER"], encoding="utf-8"):
        row = json.loads(line)
        if (
            row.get("creative_id") == os.environ["CREATIVE_ID"]
            and row.get("creative_output") == os.environ["CREATIVE_OUTPUT"]
            and str(row.get("action", "")).startswith("posted")
        ):
            found = True
except FileNotFoundError:
    pass
print("1" if found else "0")
' 2>>"$LOG")"
export LM_DAILY_CREATIVE_ALREADY_POSTED

if [ "${LM_DAILY_GENERATION_ONLY:-0}" = "1" ]; then
  PROMPT="GENERATION-ONLY 9b. Do not post, open a browser, send Telegram, invoke launchctl, or
invoke life-manager-daily.sh. This process is already the single active daily pass. Validate the
exact MP4 $LM_DAILY_VIDEO for creative $LM_DAILY_CREATIVE_ID with ffprobe and a full decode.
Read only the canonical 16-row bank at $BANK_PATH and append-only rotation state at $ROTATION_STATE
to confirm this output is the current idempotent daily selection. Do not search other directories.
Do not mutate either file. Return the required final schema JSON
immediately after those bounded checks, with concrete evidence for codec, dimensions, audio,
duration, decode exit, creative id, and output path."
else
  if [ -f "$LM_DATA_ROOT/.env" ]; then
    set +u
    set -a
    # shellcheck disable=SC1091
    . "$LM_DATA_ROOT/.env"
    set +a
    set -u
  fi
  set +e
  DISTRIBUTION_RESULT="$("$VIDEO_DISTRIBUTOR" \
    --creative-id "$LM_DAILY_CREATIVE_ID" \
    --video "$LM_DAILY_VIDEO" 2>>"$LOG")"
  DISTRIBUTION_RC=$?
  set -e
  if [ "$DISTRIBUTION_RC" -ne 0 ]; then
    printf 'daily distribution failed rc=%s; agent not invoked\n' "$DISTRIBUTION_RC" >>"$LOG"
    exit "$DISTRIBUTION_RC"
  fi
  set +e
  DISTRIBUTION_FIELDS="$(DISTRIBUTION_RESULT="$DISTRIBUTION_RESULT" EXPECTED_CREATIVE="$LM_DAILY_CREATIVE_ID" "$PYTHON" -c '
import json, os
lines = [line for line in os.environ["DISTRIBUTION_RESULT"].splitlines() if line.strip()]
if len(lines) != 1:
    raise SystemExit(2)
row = json.loads(lines[0])
if row.get("creative_id") != os.environ["EXPECTED_CREATIVE"]:
    raise SystemExit(2)
ig = row.get("instagram_url")
tt = row.get("tiktok_url")
if not isinstance(ig, str) or not ig.startswith("https://") or not isinstance(tt, str) or not tt.startswith("https://"):
    raise SystemExit(2)
print(ig + "\t" + tt)
' 2>>"$LOG")"
  DISTRIBUTION_PARSE_RC=$?
  set -e
  if [ "$DISTRIBUTION_PARSE_RC" -ne 0 ]; then
    printf 'daily distribution returned invalid result rc=%s; agent not invoked\n' "$DISTRIBUTION_PARSE_RC" >>"$LOG"
    exit 70
  fi
  IFS=$'\t' read -r LM_DAILY_INSTAGRAM_URL LM_DAILY_TIKTOK_URL <<<"$DISTRIBUTION_FIELDS"
  export LM_DAILY_INSTAGRAM_URL LM_DAILY_TIKTOK_URL
  printf 'daily distribution readback complete creative=%s\n' "$LM_DAILY_CREATIVE_ID" >>"$LOG"

  set +e
  SELF_IMPROVE_RESULT="$("$MARKETING_SELF_IMPROVER" 2>>"$LOG")"
  SELF_IMPROVE_RC=$?
  set -e
  if [ "$SELF_IMPROVE_RC" -ne 0 ]; then
    printf 'daily self-improvement metrics failed rc=%s; agent not invoked\n' "$SELF_IMPROVE_RC" >>"$LOG"
    exit "$SELF_IMPROVE_RC"
  fi
  set +e
  SELF_IMPROVE_FIELDS="$(SELF_IMPROVE_RESULT="$SELF_IMPROVE_RESULT" EXPECTED_CREATIVE="$LM_DAILY_CREATIVE_ID" "$PYTHON" -c '
import json, os
lines = [line for line in os.environ["SELF_IMPROVE_RESULT"].splitlines() if line.strip()]
if len(lines) != 1:
    raise SystemExit(2)
row = json.loads(lines[0])
if row.get("creative_id") != os.environ["EXPECTED_CREATIVE"]:
    raise SystemExit(2)
status = row.get("status")
day_index = row.get("day_index")
next_creative = row.get("next_creative_id")
reason = row.get("next_change_reason")
if status not in {"started", "done"} or not isinstance(day_index, int) or day_index < 1:
    raise SystemExit(2)
if not isinstance(next_creative, str) or not next_creative or not isinstance(reason, str) or not reason:
    raise SystemExit(2)
print("\t".join((status, str(day_index), next_creative, reason)))
' 2>>"$LOG")"
  SELF_IMPROVE_PARSE_RC=$?
  set -e
  if [ "$SELF_IMPROVE_PARSE_RC" -ne 0 ]; then
    printf 'daily self-improvement returned invalid result rc=%s; agent not invoked\n' "$SELF_IMPROVE_PARSE_RC" >>"$LOG"
    exit 70
  fi
  IFS=$'\t' read -r LM_SELF_IMPROVE_STATUS LM_SELF_IMPROVE_DAY_INDEX \
    LM_SELF_IMPROVE_NEXT_CREATIVE LM_SELF_IMPROVE_REASON <<<"$SELF_IMPROVE_FIELDS"
  export LM_SELF_IMPROVE_STATUS LM_SELF_IMPROVE_DAY_INDEX \
    LM_SELF_IMPROVE_NEXT_CREATIVE LM_SELF_IMPROVE_REASON
  printf 'daily self-improvement readback complete day=%s status=%s\n' \
    "$LM_SELF_IMPROVE_DAY_INDEX" "$LM_SELF_IMPROVE_STATUS" >>"$LOG"

  PROMPT="Run ONE bounded daily Life Manager marketing pass with no human in the loop.
This is the existing ai.anicca.life-manager-daily route. Preserve its Reddit karma gate, CEO
report, cost recording, Telegram report and logged-out verification. Do not create a new account
or a new marketing loop.
Do not invoke life-manager-daily.sh or launchctl: this process is already the one active daily pass.
Do not inspect, monitor, or wait for this active process, its PID, launchd state, or evidence files;
that creates a self-deadlock. Do not sleep or start background work. Finish this bounded pass directly.

DAILY VIDEO CONTRACT: the exact MP4 is $LM_DAILY_VIDEO and the exact creative id is
$LM_DAILY_CREATIVE_ID (duration $LM_DAILY_VIDEO_DURATION seconds).
DETERMINISTIC DISTRIBUTION COMPLETE: the exact same video and caption contract is already published
through the existing Life Manager Instagram file/script route at $LM_DAILY_INSTAGRAM_URL and the
existing TikTok Postiz integration at $LM_DAILY_TIKTOK_URL. The distribution ledger binds both URLs
to the same creative id plus video/caption SHA-256. Treat those two URLs as immutable input and do
not repost either platform. Continue only the existing Reddit gate, CEO/cost ledger and one-screen
Telegram report. On any later failure, report it honestly; never turn an internal failure into
success.
SELF-IMPROVEMENT LEDGER RECORDED: real public metrics day $LM_SELF_IMPROVE_DAY_INDEX has status
$LM_SELF_IMPROVE_STATUS. The next creative is $LM_SELF_IMPROVE_NEXT_CREATIVE because:
$LM_SELF_IMPROVE_REASON. This append-only measurement is already complete. Do not invent, alter,
backfill, or duplicate metrics."
fi

EVIDENCE_DIR="${LM_DAILY_EVIDENCE_DIR:-$LM_DATA_ROOT/state/agent-runner-evidence/life-manager-daily/$(date +%s)-$$}"
export ANICCA_USAGE_LEDGER="$USAGE_LEDGER"
set +e
printf '%s\n' "$PROMPT" | "$RUN_AGENT" --task-class marketing-agent \
  --evidence-dir "$EVIDENCE_DIR" --task-label life-manager-daily --loop life-manager >>"$LOG" 2>&1
RUNNER_RC=$?
set -e

SUMMARY="$EVIDENCE_DIR/summary.json"
EFFECTIVE_RC="$RUNNER_RC"
if [ "$RUNNER_RC" -eq 0 ]; then
  if ! "$PYTHON" - "$SUMMARY" <<'PY' >/dev/null 2>&1
import json, pathlib, sys
summary = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if summary.get("status") != "success":
    raise SystemExit(1)
if summary.get("selected_provider") != "codex" or summary.get("selected_model") != "gpt-5.6-luna":
    raise SystemExit(1)
PY
  then
    EFFECTIVE_RC=70
    printf 'runner success rejected: missing success summary or non-Luna runtime\n' >>"$LOG"
  fi
fi

SUMMARY="$SUMMARY" RUN_LEDGER="$RUN_LEDGER" CREATIVE_ID="$LM_DAILY_CREATIVE_ID" \
CREATIVE_OUTPUT="$LM_DAILY_VIDEO" DURATION="$LM_DAILY_VIDEO_DURATION" EXIT_CODE="$EFFECTIVE_RC" \
"$PYTHON" - <<'PY'
import json, os, pathlib
from datetime import datetime, timezone

summary_path = pathlib.Path(os.environ["SUMMARY"])
summary = {}
if summary_path.is_file():
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
attempt = {}
attempts_path = summary.get("attempts_path")
if attempts_path and pathlib.Path(attempts_path).is_file():
    lines = pathlib.Path(attempts_path).read_text(encoding="utf-8").splitlines()
    if lines:
        attempt = json.loads(lines[-1])
usage = attempt.get("usage") or summary.get("usage") or {}
cost_tier = attempt.get("cost_tier") or summary.get("cost_tier")
if not cost_tier and usage.get("cost_basis") == "subscription":
    cost_tier = "subscription"
provider_cost = usage.get("provider_cost_usd")
marginal_cost = 0 if cost_tier == "subscription" else provider_cost
exit_code = int(os.environ["EXIT_CODE"])
row = {
    "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "status": "success" if exit_code == 0 else "failed",
    "exit_code": exit_code,
    "provider": summary.get("selected_provider") or attempt.get("provider"),
    "model": summary.get("selected_model") or attempt.get("model"),
    "effort": summary.get("selected_effort") or attempt.get("effort"),
    "attempt_count": summary.get("attempt_count"),
    "creative_id": os.environ["CREATIVE_ID"],
    "creative_output": os.environ["CREATIVE_OUTPUT"],
    "duration_seconds": float(os.environ["DURATION"]),
    "provider_cost_usd": provider_cost,
    "cost_basis": usage.get("cost_basis", "unavailable"),
    "cost_tier": cost_tier,
    "marginal_cost_usd": marginal_cost,
}
ledger = pathlib.Path(os.environ["RUN_LEDGER"])
ledger.parent.mkdir(parents=True, exist_ok=True)
with ledger.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
PY

printf '=== life-manager-daily done rc=%s %s ===\n' "$EFFECTIVE_RC" "$(date '+%F %T %Z')" >>"$LOG"
if [ "$EFFECTIVE_RC" -eq 0 ]; then
  mkdir -p "$LM_DATA_ROOT/state"
  touch "$LM_DATA_ROOT/state/.life-manager-core-last-pass"
fi
exit "$EFFECTIVE_RC"
