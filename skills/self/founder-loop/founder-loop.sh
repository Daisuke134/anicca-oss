#!/usr/bin/env bash
# founder-loop.sh — ONE no-human wake of the founder MONEY LOOP (GLVS / HARD 0.40).
# Restore STATE → run the verified recorder (record-earn = the ONLY writer of the ledger, with all anti-fake gates,
# INV-1..7) → check THE GOAL on the REAL ledger → write STATE.md atomically → report. A cadence (/loop, cron, launchd)
# wraps this. The harness NEVER appends an earn row itself (INV-H2) and NEVER prompts a human (INV-H4).
set -uo pipefail
# launchd's default env is PATH=/usr/bin:/bin:/usr/sbin:/sbin (no node/homebrew) -- without this,
# a launchd-scheduled run silently dies at `node "$RECORD"` with "node: command not found" (rc 127),
# STATE.md never gets rewritten, and the Cadence Contract keeps missing every day even though a
# manual (interactive-shell) invocation works fine. Matches every sibling self/*.sh launchd script.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORD="$HERE/record-earn.mjs"

# launchd supplies one absolute, per-loop root; tests retain the narrower seam.
if [ "${FOUNDER_TEST:-}" = "1" ] && [ -n "${FOUNDER_DIR:-}" ]; then
  DIR="$FOUNDER_DIR"
else
  DIR="${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/founder-loop-cadence}"
fi
[[ "$DIR" = /* ]] || { echo "founder-loop: LIFE_MANAGER_STATE_ROOT must be absolute" >&2; exit 2; }
STATE_MD="$DIR/STATE.md"
# FIND-901: FOUNDER_LEDGER is a TEST-only seam, mirroring record-earn. In prod the goal-check reads the env-INDEPENDENT
# ledger path, so no env var can point it at an attacker-controlled file full of fake earnings.
if [ "${FOUNDER_TEST:-}" = "1" ] && [ -n "${FOUNDER_LEDGER:-}" ]; then LEDGER="$FOUNDER_LEDGER"; else LEDGER="$DIR/state/earn-ledger.jsonl"; fi
mkdir -p "$DIR/state"

# INV-H1: read prior STATE (the durable spine) BEFORE acting.
PREV_EARN=0
[ -f "$STATE_MD" ] && PREV_EARN="$(grep -E '^realised_earn_usdc:' "$STATE_MD" 2>/dev/null | awk '{print $2}' | tail -1)"
PREV_EARN="${PREV_EARN:-0}"

# INV-H2 + INV-H4: ONLY record-earn writes the ledger (its gates decide if a REAL external earn is recorded). No stdin.
node "$RECORD" --source x402 --wake "$(date +%s)" </dev/null >>"$DIR/state/wake.log" 2>&1; RC=$?
# FIND-1001: cap wake.log so an unattended cadence never grows it unbounded (disk hygiene, HARD 0.26).
if [ -f "$DIR/state/wake.log" ]; then tail -n 2000 "$DIR/state/wake.log" > "$DIR/state/wake.log.tmp.$$" 2>/dev/null && mv "$DIR/state/wake.log.tmp.$$" "$DIR/state/wake.log"; fi

# INV-H5: goal-check on the REAL ledger — realised earn = SUM of earn_usdc, NEVER "the wake ran".
TOTAL=0; LEDGER_OK=1
if [ -f "$LEDGER" ]; then
  TOTAL="$(jq -s 'map(.earn_usdc) | add // 0' "$LEDGER" 2>/dev/null)" || LEDGER_OK=0   # FIND-903: jq error (corrupt/malformed row) → surface, don't silently report 0
fi
printf '%s' "$TOTAL" | grep -qE '^[0-9]+(\.[0-9]+)?$' || { LEDGER_OK=0; TOTAL=0; }       # only a clean number is a real total
if [ "$LEDGER_OK" != "1" ]; then
  STATUS="LEDGER UNREADABLE — fail-safe; a real total cannot be summed, recompute before trusting"; [ "$RC" = "0" ] && RC=1
elif awk "BEGIN{exit !($TOTAL>0)}" 2>/dev/null; then STATUS="EARNING — real external USDC received; keep growing"
else STATUS="NO realised external earn yet — bottleneck is DEMAND/LISTING/PRICING, not code"; fi

# INV-H3: write STATE.md atomically (tmp + mv).
TMP="$STATE_MD.tmp.$$"
{
  echo "# Founder money loop — STATE (GLVS, no-human)"
  echo "goal: real EXTERNAL USDC to founder 0x810f6d61f7606deee2657d3083e150a222bc29c5 — Done ONLY on a verifiable on-chain receipt, NEVER 'the wake ran'"
  echo "last_wake_utc: $(date -u +%FT%TZ)"
  echo "realised_earn_usdc: $TOTAL"
  echo "prev_realised_earn_usdc: $PREV_EARN"
  echo "last_record_rc: $RC"
  echo "status: $STATUS"
  echo "next: G1.2 — host serve.mjs (X402_PAYTO=0x810f) + LIST on x402scan/Bazaar → a REAL external buyer pays → record-earn writes the first real row"
} > "$TMP" && mv "$TMP" "$STATE_MD"

# REQ-LV-018 (P0-6): report this wake by mail, using report-args.mjs's pure arg-builder to turn
# PREV_EARN/TOTAL/STATUS (already computed above, no new judgment here) into loop-report.sh's
# result/earned_usdc/evidence args. Runs AFTER STATE.md is durably written (INV-H3 unaffected).
# Does NOT touch $RECORD/$LEDGER — record-earn.mjs remains the sole ledger writer (INV-H2).
REPORT_ARGS_MJS="$HERE/report-args.mjs"
REPORT_JSON="$(node --input-type=module -e "
import { founderReportArgs } from '$REPORT_ARGS_MJS';
const [prev, total, status] = process.argv.slice(1);
console.log(JSON.stringify(founderReportArgs(Number(prev), Number(total), status)));
" -- "$PREV_EARN" "$TOTAL" "$STATUS" 2>/dev/null)"
if [ -n "$REPORT_JSON" ]; then
  R_RESULT="$(printf '%s' "$REPORT_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['result'])" 2>/dev/null)"
  R_EARNED="$(printf '%s' "$REPORT_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['earned_usdc'])" 2>/dev/null)"
  R_EVIDENCE="$(printf '%s' "$REPORT_JSON" | python3 -c "import json,sys;print(json.load(sys.stdin)['evidence'])" 2>/dev/null)"
  if [ -n "$R_RESULT" ]; then
    bash "$HERE/../../report/loop-report.sh" founder "$STATUS" "$R_RESULT" "$R_EARNED" "$R_EVIDENCE" >>"$DIR/state/wake.log" 2>&1 || true
  fi
fi

echo "founder-loop wake: realised_earn_usdc=$TOTAL record_rc=$RC state=$STATE_MD"

# REQ-CEO-070: CEO pass runs on EVERY wake, regardless of $RC (record-earn.mjs may have failed this
# wake) — a persistent RPC outage must not also starve the CEO's portfolio-allocation loop of its
# weekly pass. This call NEVER touches $RECORD/$LEDGER (INV-H2 unaffected) and its own exit status is
# always discarded (`|| true`) so it can never change founder-loop.sh's own exit code (INV-H6).
bash "$HERE/ceo/ceo-pass.sh" || true

# INV-H6: surface the recorder's rc to the cadence (a persistent RPC-fail / corrupt-cursor should alert), AFTER STATE is durably written.
exit "$RC"
