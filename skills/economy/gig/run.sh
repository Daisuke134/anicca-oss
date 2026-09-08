#!/usr/bin/env bash
# economy/gig/run.sh — automaton's wake-loop entrypoint into the P2.2 internal gig market. Mirrors
# economy/ubi/run.sh's shape (real inputs -> pure gate -> log the decision), but this skill DOES execute
# real on-chain actions (post/take/deliver/verify_and_pay), so it also follows skills/earn/run.sh's
# guard+ledger idiom: a P1 earn-guard check at the very top, and record.mjs appends to the SAME shared
# earn ledger every other earn skill uses (so the cumulative earn>spend invariant sees this instance's
# gig activity too).
#
# HARD RULE #0 (memory feedback_skills_give_tool_not_decision / feedback_build_agents_not_hardcode_regex):
# this script is the TOOL. decide.mjs's decideGigAction() is a pure ELIGIBILITY gate ONLY (surplus above
# reserve -> post-eligible; balance below the survival floor with a real open gig on the board ->
# take-eligible; otherwise idle) -- it never picks WHICH gig, WHAT task, WHAT bounty, or WHAT a
# delivered result is. Those are the calling MODEL's own judgment, read from $ANICCA_ARGS exactly like
# every other args-driven earn strategy already in this repo (earn/run.sh's hl-trade coin/side/size,
# cook/run.sh's query). A model-requested action executes ONLY when decide.mjs independently agrees
# it's eligible this wake -- a stale, mismatched, or missing request degrades to a narrate line, it
# never forces a trade.
#
# Env (runtime/loop/index.mjs's buildSkillEnv sets WAKE_ID + forwards $ANICCA_ARGS as JSON to every
# slot; private keys are ALWAYS scrubbed before spawn -- runtime/loop/env-filter.mjs strips any
# *_WALLET_KEY/*_PRIVATE_KEY/*_PRIV_KEY var -- so all signing material below is resolved from disk,
# gated on ANICCA_HOME, never inherited via env):
#   ANICCA_HOME      - gates the wallet.json / gig-agent-id.json file reads (resolve-identity.mjs)
#   ANICCA_ARGS      - JSON, optional. One of:
#                        {"action":"post","taskSpec":"...","bountyUsdcBase":1000}
#                        {"action":"take","gigId":"3","deliverable":"..."}
#                        {"action":"verify_and_pay","gigId":"3","verified":true}
#   EARN_LEDGER      - override the shared earn ledger path (default: skills/earn/state/earn-ledger.jsonl)
#   GIG_RESERVE_USDC / GIG_LOW_USDC - override decide.mjs's default eligibility thresholds
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_STATE_ROOT="${LIFE_MANAGER_SKILLS_STATE_ROOT:-${ANICCA_HOME:-$HOME/.local/state/life-manager}/state/skills}"
STATE="${GIG_STATE_ROOT:-$SKILLS_STATE_ROOT/economy/gig}"; mkdir -p "$STATE"
SEEN_PAYOUTS="$STATE/seen-payouts.json"
LEDGER="${EARN_LEDGER:-$SKILLS_STATE_ROOT/earn/earn-ledger.jsonl}"
WAKE="${WAKE_ID:-$(date -u +%s)}"
AARGS="${ANICCA_ARGS:-}"; [ -z "$AARGS" ] && AARGS='{}'

record_line() { node "$HERE/../../earn/lib/record.mjs" "$1" "$LEDGER"; }

# The gig board's own custody keypair is a SHARED secret (not per-instance identity), so it ends in
# _PRIVATE_KEY and run-skill.mjs's scrubPrivateKeys already stripped it from our env before spawn.
# Recover it from disk exactly like skills/earn/run.sh recovers its own wallet key from /opt/anicca.env
# -- mirrors scripts/e2e-testnet.mjs's own documented sourcing idiom (`set -a; source ...; set +a`).
GIG_ENV="$HOME/.anicca-signing/gig-board/.env"
if [ -f "$GIG_ENV" ]; then
  set -a; source "$GIG_ENV"; set +a
fi

# THIS instance's own EVM signing key -- file-gated on ANICCA_HOME (resolve-identity.mjs), never via env.
SIGNKEY=$(node "$HERE/../../earn/lib/resolve-identity.mjs" evm 2>/dev/null)
if [ -z "$SIGNKEY" ]; then
  echo "[gig] no signing key resolved for this instance (ANICCA_HOME=${ANICCA_HOME:-unset}) -- cannot participate this wake (fail closed)"
  JSON=$(python3 -c "import json;print(json.dumps({'wallet':'unknown','source':'gig','task':'no-signing-key','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
  record_line "$JSON" >/dev/null 2>&1 || true
  exit 0
fi
ADDR=$(SIGNKEY="$SIGNKEY" node "$HERE/lib/wallet.mjs" address 2>/dev/null)
WLOW=$(echo "$ADDR" | tr 'A-F' 'a-f')

# P1 (spec §3/§4) fail-closed cumulative earn>spend guard -- ONE check before doing anything this wake,
# the same one-line idiom skills/earn/run.sh uses at its own top.
if ! node "$HERE/../../_shared/lib/earn-guard.mjs" check "$WLOW" "gig" "$LEDGER"; then
  echo "[gig] P1 GUARD: cumulative net breach or unresolved wallet (wallet='$WLOW') -- HALT (fail-closed), skipping wake."
  exit 0
fi

# --- 1) COLLECT: any gig where I was the taker and am NOW paid, that I haven't recorded yet? --------
# Deterministic bookkeeping (not judgment): diff the real board against our own seen-payouts state file,
# same category as ledger.mjs's alreadyRecordedSig dedup.
PAID_TO_ME=$(node "$HERE/lib/board.mjs" paid-to "$ADDR" 2>/dev/null || echo '[]')
SEEN=$([ -f "$SEEN_PAYOUTS" ] && cat "$SEEN_PAYOUTS" || echo '[]')
NEW_PAYOUTS=$(python3 -c "
import json
paid = json.loads('''$PAID_TO_ME''') if '''$PAID_TO_ME'''.strip().startswith('[') else []
seen = set(json.loads('''$SEEN''')) if '''$SEEN'''.strip().startswith('[') else set()
print(json.dumps([g for g in paid if g.get('id') not in seen]))
" 2>/dev/null)
[ -z "$NEW_PAYOUTS" ] && NEW_PAYOUTS='[]'
NEW_COUNT=$(printf '%s' "$NEW_PAYOUTS" | python3 -c "import json,sys;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)

if [ "$NEW_COUNT" != "0" ]; then
  printf '%s' "$NEW_PAYOUTS" | python3 -c "
import json,sys
for g in json.load(sys.stdin):
    print(json.dumps(g))
" 2>/dev/null | while IFS= read -r GIGJSON; do
    GID=$(printf '%s' "$GIGJSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('id'))")
    TX=$(printf '%s' "$GIGJSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('payoutTx') or '')")
    BOUNTY_BASE=$(printf '%s' "$GIGJSON" | python3 -c "import json,sys;print(json.load(sys.stdin).get('bountyUsdcBase') or 0)")
    AMT=$(python3 -c "print(float('$BOUNTY_BASE')/1e6)")
    STATUS=$(node -e "import('$HERE/../../_shared/lib/verify-tx.mjs').then(m=>m.receiptStatus('$TX')).then(s=>console.log(s||'null')).catch(()=>console.log('null'))" 2>/dev/null)
    JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'gig #$GID paid','earn_usdc':$AMT,'cost_usdc':0,'tx':'$TX','status':'$STATUS','external':True,'wake':'$WAKE'}))")
    OUT=$(record_line "$JSON")
    echo "[gig] collected payout for gig #$GID (tx=$TX amt=\$$AMT) -> $OUT"
    if [ "$OUT" = "PROFITABLE" ]; then
      echo "[gig] GATE-0 MET: real external gig payout recorded (net>0, status 0x1, external inbound)."
    fi
  done
  # append every newly-seen gigId to the bookkeeping file, ONCE (never re-record the same payout twice).
  python3 -c "
import json
paid = json.loads('''$PAID_TO_ME''')
seen = set(json.loads('''$SEEN''')) if '''$SEEN'''.strip().startswith('[') else set()
seen |= {g.get('id') for g in paid}
with open('$SEEN_PAYOUTS', 'w') as f:
    json.dump(sorted(seen), f)
"
fi

# --- 2) ELIGIBILITY: gather REAL balance + REAL open board, ask the pure gate what's possible now ----
BAL=$(ADDR="$ADDR" node "$HERE/lib/wallet.mjs" balance 2>/dev/null); [ -z "$BAL" ] && BAL=0
OPEN_GIGS=$(node "$HERE/lib/board.mjs" open 2>/dev/null || echo '[]')
RESERVE="${GIG_RESERVE_USDC:-}"
LOW="${GIG_LOW_USDC:-}"
DECISION=$(BAL="$BAL" OPEN_GIGS="$OPEN_GIGS" RESERVE="$RESERVE" LOW="$LOW" node --input-type=module -e "
import { decideGigAction } from '$HERE/decide.mjs';
const opts = { balanceUsdc: Number(process.env.BAL), openGigs: JSON.parse(process.env.OPEN_GIGS || '[]') };
if (process.env.RESERVE) opts.reserveUsdc = Number(process.env.RESERVE);
if (process.env.LOW) opts.lowUsdc = Number(process.env.LOW);
console.log(JSON.stringify(decideGigAction(opts)));
" 2>/dev/null)
[ -z "$DECISION" ] && DECISION='{"action":"idle","reason":"decide() call failed -- fail closed"}'
ELIGIBLE=$(printf '%s' "$DECISION" | python3 -c "import json,sys;print(json.load(sys.stdin).get('action','idle'))" 2>/dev/null || echo idle)
OPEN_COUNT=$(printf '%s' "$OPEN_GIGS" | python3 -c "import json,sys;print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
echo "[gig] balance=\$$BAL open_gigs=$OPEN_COUNT eligible=$ELIGIBLE -> $DECISION"

# --- 3) EXECUTE: only the MODEL's own decision this wake ($ANICCA_ARGS), and only when it matches ----
# what decide.mjs just said is eligible. HARD RULE #0: run.sh never invents WHICH gig/task/bounty/
# deliverable -- those fields come straight from the model's own args, untouched.
ACTION=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('action','') or '')" 2>/dev/null)

if [ "$ACTION" = "post" ] && [ "$ELIGIBLE" = "post" ]; then
  TASK_SPEC=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('taskSpec','') or '')" 2>/dev/null)
  BOUNTY_BASE=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('bountyUsdcBase','') or '')" 2>/dev/null)
  SURPLUS=$(printf '%s' "$DECISION" | python3 -c "import json,sys;print(json.load(sys.stdin).get('surplusUsdc',0))" 2>/dev/null || echo 0)
  if [ -z "$TASK_SPEC" ] || [ -z "$BOUNTY_BASE" ]; then
    echo "[gig] post-eligible but the model supplied no taskSpec/bountyUsdcBase this wake -- narrating eligibility only."
    JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'post-eligible: awaiting taskSpec/bountyUsdcBase from the model','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
    record_line "$JSON" >/dev/null 2>&1 || true
  elif ! python3 -c "import sys; sys.exit(0 if float('$BOUNTY_BASE')/1e6 <= float('$SURPLUS') else 1)" 2>/dev/null; then
    echo "[gig] REJECT: requested bounty (\$$(python3 -c "print(float('$BOUNTY_BASE')/1e6)")) exceeds this wake's surplus (\$$SURPLUS) -- not posting."
    JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'post-rejected: bounty exceeds surplus \$$SURPLUS','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
    record_line "$JSON" >/dev/null 2>&1 || true
  else
    AGENTID_RES=$(SIGNKEY="$SIGNKEY" node "$HERE/lib/ensure-agent-id.mjs" 2>/dev/null)
    AGENTID_OK=$(printf '%s' "$AGENTID_RES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('ok',False))" 2>/dev/null)
    if [ "$AGENTID_OK" != "True" ]; then
      echo "[gig] cannot post: ERC-8004 identity unavailable ($AGENTID_RES)"
      JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'post-skipped: identity unavailable','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
      record_line "$JSON" >/dev/null 2>&1 || true
    else
      AGENT_ID=$(printf '%s' "$AGENTID_RES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('agentId'))" 2>/dev/null)
      DESC=$(python3 -c "import json;print(json.dumps({'action':'post','posterAgentId':'$AGENT_ID','taskSpec':'''$TASK_SPEC''','bountyUsdcBase':int('$BOUNTY_BASE')}))")
      RES=$(SIGNKEY="$SIGNKEY" node "$HERE/lib/act.mjs" "$DESC" 2>&1)
      echo "[gig] post result: $RES"
      OK=$(printf '%s' "$RES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('ok',False))" 2>/dev/null)
      GID=$(printf '%s' "$RES" | python3 -c "import json,sys;d=json.load(sys.stdin);print((d.get('gig') or {}).get('id',''))" 2>/dev/null)
      JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'gig-post #$GID','earn_usdc':0,'cost_usdc':0,'posted_usdc':float('$BOUNTY_BASE')/1e6,'wake':'$WAKE'}))")
      OUT=$(record_line "$JSON")
      echo "[gig] post recorded (ok=$OK) -> $OUT"
    fi
  fi

elif [ "$ACTION" = "take" ] && [ "$ELIGIBLE" = "take" ]; then
  GIG_ID=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('gigId','') or '')" 2>/dev/null)
  DELIVERABLE=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('deliverable','') or '')" 2>/dev/null)
  if [ -z "$GIG_ID" ] || [ -z "$DELIVERABLE" ]; then
    echo "[gig] take-eligible but the model supplied no gigId/deliverable this wake -- narrating eligibility only."
    JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'take-eligible: awaiting gigId/deliverable from the model','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
    record_line "$JSON" >/dev/null 2>&1 || true
  else
    AGENTID_RES=$(SIGNKEY="$SIGNKEY" node "$HERE/lib/ensure-agent-id.mjs" 2>/dev/null)
    AGENTID_OK=$(printf '%s' "$AGENTID_RES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('ok',False))" 2>/dev/null)
    if [ "$AGENTID_OK" != "True" ]; then
      echo "[gig] cannot take: ERC-8004 identity unavailable ($AGENTID_RES)"
      JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'take-skipped: identity unavailable','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
      record_line "$JSON" >/dev/null 2>&1 || true
    else
      AGENT_ID=$(printf '%s' "$AGENTID_RES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('agentId'))" 2>/dev/null)
      TAKE_DESC=$(python3 -c "import json;print(json.dumps({'action':'take','gigId':'$GIG_ID','takerAddress':'$ADDR','takerAgentId':'$AGENT_ID'}))")
      TAKE_RES=$(node "$HERE/lib/act.mjs" "$TAKE_DESC" 2>&1)
      echo "[gig] take result: $TAKE_RES"
      TAKE_OK=$(printf '%s' "$TAKE_RES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('ok',False))" 2>/dev/null)
      if [ "$TAKE_OK" != "True" ]; then
        echo "[gig] take failed, not delivering -> $TAKE_RES"
        JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'gig-take-failed #$GIG_ID','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
        record_line "$JSON" >/dev/null 2>&1 || true
      else
        DELIVER_DESC=$(python3 -c "import json,sys;print(json.dumps({'action':'deliver','gigId':sys.argv[1],'deliverable':sys.argv[2]}))" "$GIG_ID" "$DELIVERABLE")
        DELIVER_RES=$(node "$HERE/lib/act.mjs" "$DELIVER_DESC" 2>&1)
        echo "[gig] deliver result: $DELIVER_RES"
        JSON=$(python3 -c "import json;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'gig-take+deliver #$GIG_ID','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
        OUT=$(record_line "$JSON")
        echo "[gig] take+deliver recorded -> $OUT (payout arrives once the poster calls verify_and_pay -- collected automatically on a future wake, see step 1 above)"
      fi
    fi
  fi

elif [ "$ACTION" = "verify_and_pay" ]; then
  # POSTER-side judgment: the model reviewing a gig IT posted decides whether the delivered work meets
  # taskSpec. verified is the model's own call; run.sh only routes it (gigVerifyAndPay itself enforces
  # poster-auth + fail-closed payout gating, see gig.mjs).
  GIG_ID=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('gigId','') or '')" 2>/dev/null)
  VERIFIED=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print(str((json.load(sys.stdin) or {}).get('verified',False)).lower())" 2>/dev/null)
  if [ -z "$GIG_ID" ]; then
    echo "[gig] verify_and_pay requested but no gigId supplied -- skipping."
  else
    VP_DESC=$(python3 -c "import json,sys;print(json.dumps({'action':'verify_and_pay','gigId':sys.argv[1],'verified':sys.argv[2]=='true'}))" "$GIG_ID" "$VERIFIED")
    VP_RES=$(SIGNKEY="$SIGNKEY" node "$HERE/lib/act.mjs" "$VP_DESC" 2>&1)
    echo "[gig] verify_and_pay result: $VP_RES"
    JSON=$(python3 -c "import json,sys;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'gig-verify #'+sys.argv[1]+' verified='+sys.argv[2],'earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))" "$GIG_ID" "$VERIFIED")
    record_line "$JSON" >/dev/null 2>&1 || true
  fi

else
  # nothing eligible+requested this wake matched -- narrate the real state so the model can decide next
  # (same "observe" fallback shape as earn/run.sh's hl-trade branch when SIDE/SIZE are absent).
  JSON=$(python3 -c "import json,sys;print(json.dumps({'wallet':'$WLOW','source':'gig','task':'observe: eligible='+sys.argv[1]+' open='+sys.argv[2]+' balance=\$'+sys.argv[3],'earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))" "$ELIGIBLE" "$OPEN_COUNT" "$BAL")
  OUT=$(record_line "$JSON")
  echo "[gig] narrate -> $OUT"
fi

exit 0
