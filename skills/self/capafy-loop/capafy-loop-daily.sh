#!/usr/bin/env bash
# Load the private launchd environment before any child process is started. Keep the source
# outside the repository, suppress dotenv output, and export every assignment so provider keys
# (including OpenRouter) reach the existing runner without ever being printed here.
LIFE_MANAGER_ENV_FILE="$HOME/.local/state/life-manager/.env"
if [ -f "$LIFE_MANAGER_ENV_FILE" ]; then
  set -a
  . "$LIFE_MANAGER_ENV_FILE" >/dev/null 2>&1 || true
  set +a
fi
if [ -n "${LM_TELEGRAM_ALERT_CHAT_ID:-}" ] && [ -z "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
  export TELEGRAM_ALERT_CHAT_ID="$LM_TELEGRAM_ALERT_CHAT_ID"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIFE_MANAGER_RELEASE_ROOT="${LIFE_MANAGER_RELEASE_ROOT:-$(cd "$SCRIPT_DIR/../../.." && pwd)}"
LIFE_MANAGER_SOURCE_REPO="${LIFE_MANAGER_SOURCE_REPO:-$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null)}"
[ -x "$LIFE_MANAGER_RELEASE_ROOT/bin/lm-loop" ] || { echo "LIFE_MANAGER_RELEASE_ROOT invalid" >&2; exit 2; }
[ -n "$LIFE_MANAGER_SOURCE_REPO" ] && [ -w "$LIFE_MANAGER_SOURCE_REPO" ] \
  && git -C "$LIFE_MANAGER_SOURCE_REPO" remote get-url origin 2>/dev/null \
  | grep -Eq '(^|[:/])Daisuke134/life-manager(\.git)?$' \
  || { echo "LIFE_MANAGER_SOURCE_REPO invalid" >&2; exit 2; }
export LIFE_MANAGER_RELEASE_ROOT LIFE_MANAGER_SOURCE_REPO
export LIFE_MANAGER_REPO="$LIFE_MANAGER_RELEASE_ROOT"
# capafy-loop-daily.sh — deterministic hourly wake for the Capafy money loop:
# found via live audit that the tmux STARTUP prompt's self-registered `CronCreate "0 9 * * *"` never
# actually persisted anywhere on this machine — no cron store, no second daily run, tmux session sat
# idle for 19h+ after its one manual pass on 2026-07-11. Root cause: an in-session CronCreate call from
# an in-session model/tmux process is not a real OS/gateway scheduler. Fix = same pattern already proven
# launchd calls THIS script directly every hour. CAP_FULL is a cheap no-op; an open
# slot is acted on within one hour without relying on an LLM to schedule itself.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
set -uo pipefail
RUN_AGENT="$SCRIPT_DIR/../../earn/marketing-engine/run_agent.sh"
LOG="$HOME/.local/state/life-manager/logs/capafy-loop-daily.log"
LAST_PASS_MARKER="$HOME/.local/state/life-manager/state/.capafy-loop-last-pass"
HEALTHY_MARKER="$HOME/.local/state/life-manager/state/capafy-autopublish/.capafy-healthy-pass"
TERMINAL_LEDGER="$HOME/.local/state/life-manager/state/capafy-daily-terminals.jsonl"
TERMINAL_TOOL="$SCRIPT_DIR/capafy_daily_terminal.py"
PASS_SCHEMA="$SCRIPT_DIR/capafy-loop-pass.schema.json"
OFFLINE_CADENCE_TOOL="$SCRIPT_DIR/capafy_offline_cadence.py"
OFFLINE_CADENCE_STATE="$HOME/.local/state/life-manager/state/capafy-offline-build-cadence.json"
SELFHEAL_REQUEST="$HOME/.local/state/life-manager/state/capafy-loop-selfheal-request.json"
EXECUTION_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
TERMINAL_RECORDED=0
mkdir -p "$(dirname "$LOG")"
echo "=== capafy-loop-daily run $(date '+%F %T %Z') ===" >>"$LOG"

mark_healthy() {
  mkdir -p "$(dirname "$LAST_PASS_MARKER")" "$(dirname "$HEALTHY_MARKER")"
  touch "$LAST_PASS_MARKER" "$HEALTHY_MARKER"
}

record_terminal() {
  local rc="$1" verdict="${2:-UNCLASSIFIED}"
  [ "$TERMINAL_RECORDED" -eq 0 ] || return 0
  python3 "$TERMINAL_TOOL" finish --ledger "$TERMINAL_LEDGER" \
    --execution-id "$EXECUTION_ID" --rc "$rc" --verdict "$verdict" >>"$LOG" 2>&1 || true
  TERMINAL_RECORDED=1
}
on_exit() {
  local rc=$?
  record_terminal "$rc" "${VERDICT:-UNCLASSIFIED}"
}
trap on_exit EXIT
python3 "$TERMINAL_TOOL" start --ledger "$TERMINAL_LEDGER" \
  --execution-id "$EXECUTION_ID" >>"$LOG" 2>&1 || exit 1

# Subscriber requests still spend hosted credits while the publish queue is full.
# Check the canonical host key before every healthy-idle exit, not only while publishing.
bash "$LIFE_MANAGER_RELEASE_ROOT/skills/capafy-autopublish/scripts/key_health_gate.sh" \
  >>"$LOG" 2>&1 || exit $?

# Enforce the five simultaneous-submission cap before spending an agent turn.
# CAP_FULL permits one offline-only candidate build per local calendar day. It never writes to Capafy.
INVENTORY="$(CAPAFY_CATALOG_DIR="$LIFE_MANAGER_SOURCE_REPO/skills/capafy/catalog" \
  python3 "$LIFE_MANAGER_RELEASE_ROOT/skills/capafy-autopublish/scripts/inventory_status.py" 2>>"$LOG")"
VERDICT="$(printf '%s\n' "$INVENTORY" | sed -n 's/^VERDICT=//p' | head -1)"
printf '%s\n' "$INVENTORY" | tail -n 1 | python3 \
  "$LIFE_MANAGER_RELEASE_ROOT/skills/capafy-autopublish/scripts/candidate_backlog.py" refresh \
  --catalog "$LIFE_MANAGER_SOURCE_REPO/skills/capafy/catalog" \
  --inventory-stdin >>"$LOG" 2>&1 || exit 1
if [ "$VERDICT" = "CAP_FULL" ] && [ ! -f "$SELFHEAL_REQUEST" ]; then
  if ! python3 "$OFFLINE_CADENCE_TOOL" claim --state "$OFFLINE_CADENCE_STATE" \
      --day "$(date +%F)" --execution-id "$EXECUTION_ID" >>"$LOG" 2>&1; then
    echo "=== capafy-loop-daily done rc=0 (HEALTHY-IDLE: CAP_FULL; offline build already claimed today; agent spend=0; platform write=0) $(date '+%F %T %Z') ===" >>"$LOG"
    mark_healthy || exit 2
    exit 0
  fi

  BACKLOG="$HOME/.local/state/life-manager/state/capafy-candidate-backlog.json"
  READY_BEFORE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("counts",{}).get("ready",0))' "$BACKLOG")"
  EVIDENCE_DIR="$HOME/.local/state/life-manager/state/agent-runner-evidence/capafy-offline-build/$(date +%s)-$$"
  OFFLINE_PROMPT='Build exactly ONE differentiated, honest Capafy skill candidate OFFLINE inside $LIFE_MANAGER_SOURCE_REPO/skills/capafy/catalog/<new-slug>/. Use only executables from $LIFE_MANAGER_RELEASE_ROOT. This is a CAP_FULL pass: NEVER call Capafy create/publish/configure/ship/submit APIs or UI, and never modify any remote platform state. Use sales_selector.py plus current inventory and existing catalog to avoid duplicates. Produce SKILL.md, LISTING.md, icon.svg, and evidence/verified-demonstration.md containing a concrete input, actual output, and verification notes. Follow the repository skill-creator quality contract, keep claims within what the skill can actually do, require No Free Trial in every plan, and run the release-owned listing lint. Commit, push, merge through a PR, and verify origin/main contains the completed source change. Return status=success only after all four repo-owned artifacts exist, lint passes, and the commit is an ancestor of origin/main; otherwise status=failure. Include the created path, commit, merged-main readback, and lint evidence.'
  printf '%s\n' "$OFFLINE_PROMPT" | AGENT_RUNNER_EVIDENCE_MIN_FREE_BYTES=67108864 "$RUN_AGENT" \
    --task-class browser-lane-agent --schema "$PASS_SCHEMA" --evidence-dir "$EVIDENCE_DIR" \
    --task-label capafy-offline-daily --loop capafy \
    --escalation-reason "authorized Capafy browser publishing workflow" >>"$LOG" 2>&1
  RC=$?
  [ "$RC" -eq 0 ] || exit "$RC"
  python3 "$TERMINAL_TOOL" result --summary "$EVIDENCE_DIR/summary.json" >>"$LOG" 2>&1 || exit $?
  printf '%s\n' "$INVENTORY" | tail -n 1 | python3 \
    "$LIFE_MANAGER_RELEASE_ROOT/skills/capafy-autopublish/scripts/candidate_backlog.py" refresh \
    --catalog "$LIFE_MANAGER_SOURCE_REPO/skills/capafy/catalog" \
    --inventory-stdin >>"$LOG" 2>&1 || exit 1
  READY_AFTER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("counts",{}).get("ready",0))' "$BACKLOG")"
  [ "$READY_AFTER" -gt "$READY_BEFORE" ] || { echo "offline build produced no new ready candidate" >>"$LOG"; exit 1; }
  VERDICT="CAP_FULL_OFFLINE_BUILT"
  echo "=== capafy-loop-daily done rc=0 (CAP_FULL_OFFLINE_BUILT; ready=$READY_BEFORE->$READY_AFTER; platform write=0) $(date '+%F %T %Z') ===" >>"$LOG"
  mark_healthy || exit 2
  exit 0
fi
if [ "$VERDICT" = "SERVER_UNREADABLE" ]; then
  echo "=== capafy-loop-daily done rc=1 (SERVER_UNREADABLE; fail closed) $(date '+%F %T %Z') ===" >>"$LOG"
  exit 1
fi

PROMPT='You are the Anicca Capafy money-loop core (claude-p, self-improving + self-healing; money → Dais bank; human + main-agent NOT in this loop). This pass was triggered by a real launchd daily schedule (ai.anicca.capafy-loop-daily) — you do NOT need to register your own cron; that is handled outside this process now. STEP0 SELF-HEAL: AUTH-DOWN means ONLY that GET https://api.capafy.ai/agent/account returns HTTP 401 — check THAT first; if it returns 200 the token is VALID and you must NOT re-login. The Capafy marketplace SEARCH endpoint (POST /agent/agents/search) may be BROKEN server-side (returns 500, or a MISLEADING 401 that says token expired, even with a valid token), and re-login just returns the SAME stable am_sk_ api-key so it can NEVER fix a search error — therefore a search failure is NEVER auth-down and NEVER a reason to re-login, retry, or spiral. Only if /agent/account itself is 401: re-login via login-init → read OTP from $LIFE_MANAGER_GMAIL_ACCOUNT via gog gmail (needs GOG_KEYRING_PASSWORD sourced from $HOME/.local/state/life-manager/.env) → login-verify → write token into BOTH vendor/capafy-publisher/config.json and vendor/capafy-user/config.json. If $HOME/.local/state/life-manager/state/capafy-loop-selfheal-request.json exists, read+diagnose+fix then rm it; CAPAFY-LOOP-STALE/NEVER-RAN → run bash $LIFE_MANAGER_REPO/skills/capafy-autopublish/scripts/daily_loop.sh; verify heal clears then rm the json; if you cannot fix it in this pass, bash $LIFE_MANAGER_REPO/skills/self/self-fix.sh capafy "<the exact blocker + a concrete fix hint>" to spawn an autonomous Opus dev that actually edits+runs+verifies+commits the code fix (THAT is the self-heal — never just file an issue and wait), then rm the json. STEP1 MEASURE: bash $LIFE_MANAGER_REPO/skills/self/capafy-loop/loop.sh and read its STATE.md. STEP2 ACT (single highest-EV move toward real Capafy revenue; your judgment): FIRST run  /opt/homebrew/bin/python3 $LIFE_MANAGER_REPO/skills/self/capafy-loop/sales_selector.py  and read its JSON (also saved to $HOME/.local/state/life-manager/state/capafy-sales-ranking.json): if signal=sales, build the NEXT skill in the winner category it names (double down on what actually sells for us); if signal=none (no sales yet across our listings), do NOT fabricate a winner — proceed via BEST_PRACTICES marketplace research below. Then improve SUPPLY QUALITY by publishing ONE more competitive, honest, lint-clean listing. Market-search (reading a top seller to clone) is BEST-EFFORT enrichment ONLY: try it at most once, but Capafy search may be server-broken so expect it to fail — if it errors, do NOT retry, re-login, or stop; proceed WITHOUT it using $LIFE_MANAGER_REPO/skills/capafy-autopublish/BEST_PRACTICES.md + the proven-profitable playbook + existing inventory. If BEST_PRACTICES.md has not been refreshed with a real external web search in the last 7 days, spend at most one WebSearch/firecrawl pass on "capafy.ai top selling AI agent skills" or similar and append any genuinely new, verifiable finding to BEST_PRACTICES.md (do not invent claims). Then publish exactly ONE ready item by running the BOUNDED drainer: bash $LIFE_MANAGER_REPO/skills/capafy-autopublish/scripts/daily_loop.sh (fail-closed, self-limited, timeout-guarded). If the slot cap is full OR no inventory item is ready (skill dir + icon both present), that is a LEGITIMATE NO-OP — report it and finish the pass cleanly; never hang waiting. If verdict=DRAINED (no publishable item and no fresh inventory), use the rest of this pass to DESIGN and FULLY SUBMIT ONE brand-new, genuinely differentiated skill (not a duplicate of an already-online listing) — write its skill dir + LISTING.md + test case + icon, run publish-init, then DRIVE IT ALL THE WAY THROUGH CP1 (agent card: title/short/detailed/welcomeMessage/tags/category/pricing/model/test-input/per-plan trial radios/DPA checkbox — read PUBLISHING_RUNBOOK.md for the complete silent-failure gotcha list) → publish-configure → CP2 (LLM config keys) → publish-ship → CP3 (審査に提出). ★★ A DRAFT IS NOT DONE. A draft earns $0 and occupies a publish-cap slot. The act is only complete when publish-remote-status shows status=1 (under review) + isConfirmedSkills=1 + isConfirmedConfigKeys=1. ★★ You have NO wall-clock limit on this pass (no timeout) — do NOT stop early, do NOT defer work to "tomorrow'"'"'s pass", do NOT leave a half-finished draft behind. If a UI step blocks you, debug it and retry until it actually saves. There is also an existing ORPHAN-DRAFT backlog on the server — resolve those the same way (finish them through CP3, or explicitly abandon them to free the cap slot); never let drafts pile up. STEP3 VERIFY: only a real subscriber or a listing reaching status=4 counts — published alone is NOT revenue; record the observable in STATE.md. IF your ACT fails because of a code/automation bug (e.g. daily_loop.sh reports publish FAIL / drive_cp1.py isConfirmedSkills=0 / any tool error), that is NOT a stop and NOT a defer — immediately bash $LIFE_MANAGER_REPO/skills/self/self-fix.sh capafy "publish pipeline broken: <paste the exact error>. Fix the publisher (likely drive_cp1.py brittle UI automation) so a real skill publishes." and note it in STATE.md. STEP4 REPORT: bash $LIFE_MANAGER_REPO/skills/report/loop-report.sh capafy "<what you did + real metric>" <success|failure|no-op> <usd or 0> "<evidence url or none>". Do not write `.capafy-loop-last-pass`; the deterministic wrapper owns that heartbeat and writes it only after this runner exits 0. Blocker is not stop; a broken Capafy endpoint is not stop. STEP5 TELEGRAM REPORT TO DAIS -- MANDATORY, every pass, success or failure: PushNotification does NOT reach Dais (it silently no-ops when Remote Control is inactive -- proven 2026-07-12). Use the repository-owned sender: bash $LIFE_MANAGER_REPO/skills/_shared/send-telegram.sh "<your honest one-screen report>" $TELEGRAM_ALERT_CHAT_ID. The message MUST contain: what you actually did this pass, the agent_id and its real remote status (status/isConfirmedSkills/isConfirmedConfigKeys read back from publish-remote-status -- never a claim without the readback), how many listings are online, and the honest revenue figure (say $0 if it is $0 -- never inflate). Confirm the send returned a real message id; if the send fails, retry once and then note the failure in STATE.md.'

EVIDENCE_DIR="$HOME/.local/state/life-manager/state/agent-runner-evidence/capafy-marketplace/$(date +%s)-$$"
PROMPT="$PROMPT RUNTIME FACT: this unique launchd owner now wakes hourly; do not create another scheduler. INITIAL WRAPPER INVENTORY VERDICT: $VERDICT. Historical log lines before this execution are not current failures; use only commands and terminal records produced at or after execution $EXECUTION_ID when diagnosing this pass. DRAINED requires designing and fully submitting one new skill; a successful DRAINED drainer is not an ENOSPC failure and is not the terminal action while a submission slot remains. SALES SIGNAL OVERRIDE: if sales_selector.py returns signal=unattributed_sales, company orders are real but no Agent winner is observable. Do not fabricate a winner, do not claim a winning category, and do not create a clone on that basis. Use tracked marketing rotation across existing online listings to create attribution; CAP_FULL remains a legitimate no-write terminal. TERMINAL STATUS RULE: return status=failure if any required action, Telegram report, or official readback failed; status=success only when required work and official effect are complete; status=no_op only for a legitimate external no-write terminal. Include at least one concrete evidence item."
PROMPT="$PROMPT RUNTIME BOUNDARY: execute code only from LIFE_MANAGER_RELEASE_ROOT; create/edit/commit/push public source only in LIFE_MANAGER_SOURCE_REPO and never write the read-only release."
# task-class browser-lane-agent (900s), not tool-agent (180s): a capafy pass
# drives the Capafy CP1/CP2/CP3 browser UI. Measured 2026-07-27 against this
# exact prompt, a READ-ONLY dry run already took 21 turns / 229s, and a real
# publish pass is longer. Under tool-agent every pass was SIGKILLed mid-flight
# -- 2026-07-24..27 attempt-01 hit rc=124 four days running with ~195KB of real
# progress on stdout -- which is why the prompt's "no wall-clock limit" promise
# was a lie the model kept planning against. Same split, same reason as the gig
# browser lanes.
printf '%s\n' "$PROMPT" | AGENT_RUNNER_EVIDENCE_MIN_FREE_BYTES=67108864 "$RUN_AGENT" \
  --task-class browser-lane-agent \
  --schema "$PASS_SCHEMA" \
  --evidence-dir "$EVIDENCE_DIR" \
  --task-label capafy-marketplace-daily \
  --loop capafy \
  --escalation-reason "authorized Capafy browser publishing workflow" >>"$LOG" 2>&1
RC=$?
echo "=== capafy-loop-daily done rc=$RC $(date '+%F %T %Z') ===" >>"$LOG"
[ "$RC" -eq 0 ] || exit "$RC"

RESULT_JSON="$(python3 "$TERMINAL_TOOL" result --summary "$EVIDENCE_DIR/summary.json" 2>>"$LOG")"
RESULT_RC=$?
RESULT_STATUS="$(printf '%s' "$RESULT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status","invalid"))' 2>/dev/null || echo invalid)"
VERDICT="RESULT_${RESULT_STATUS}"
echo "=== capafy-loop-daily result status=$RESULT_STATUS mapped_rc=$RESULT_RC ===" >>"$LOG"
[ "$RESULT_RC" -eq 0 ] || exit "$RESULT_RC"
mark_healthy || exit 2
exit 0
