#!/usr/bin/env bash
# Anicca earn skill — GATE-0 entrypoint. Called by the automaton loop each wake.
# No human, no Claude in the loop: the agent picks a source + executes the earn (on-chain),
# this harness VERIFIES it (tx receipt 0x1 + USDC before/after delta) and appends ONE
# line to state/earn-ledger.jsonl. A profitable wake (net>0 AND status 0x1) = the launch gate.
#
# Modes (set by the caller / automaton loop via env):
#   discover (default)  — no executed earn this wake: writes a narrate line (no tx). exit 0.
#   execute             — perform/verify a real on-chain earn this wake and record it:
#     - EARN_STRATEGY=swap (default for the loop): run.sh ITSELF executes a real Uniswap V3
#       ETH->USDC swap (execute-swap.py), then records the receipt + real USDC delta. GATE-0.
#     - EARN_TX preset (externally-executed earn, e.g. x402): verify that tx, record it.
#
# Env contract (mirrors the proven report skill):
#   /opt/anicca.env (if present) is sourced. Needs the wallet privkey var named by PKVAR
#   (default BLOCKRUN_WALLET_KEY) to derive the wallet address for the USDC delta proof.
#   Optional: BASE_RPC_URL, USDC_ADDRESS, EARN_LEDGER (override ledger path).
#   Swap: EARN_SWAP_ETH (0.0003), EARN_SLIPPAGE_BPS (100), EARN_MIN_ETH_RESERVE (0.0005).
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Env discovery: hosted installs may ship /opt/anicca.env; local installs use the
# canonical Life Manager environment. Source only those declared installation boundaries.
# Allowlist ONLY the vars the earn path needs — never expose user-PII env (gmail/gcal/composio/
# google-login) to the earn process, or identity-guard.mjs (malice-guard) fails closed and HALTS.
# Reconciled against every env var read by run.sh + execute-0xwork.py (verified 2026-06-16).
EARN_ALLOW="BLOCKRUN_WALLET_KEY PKVAR OXWORK_PKVAR BASE_RPC_URL USDC_ADDRESS EARN_MODE EARN_STRATEGY EARN_TX EARN_SOURCE EARN_AMOUNT EARN_COST EARN_TASK EARN_LEDGER WAKE_ID OXWORK_API OXWORK_CAPS OXWORK_DELIVER OXWORK_POLL_SECS OXWORK_ANY_CATEGORY OXWORK_TASK_ID AUTO_CANCEL_USDC SUB_ID SELF_CANCEL_TOKEN ANICCA_API_BASE"
for ENVF in /opt/anicca.env "${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"; do
  [ -f "$ENVF" ] || continue
  while IFS= read -r kv; do
    k="${kv%%=*}"
    case " $EARN_ALLOW " in *" $k "*) export "$kv" ;; esac
  done < <(grep -E '^[A-Z_]+=' "$ENVF")
done
# Defense-in-depth: UNSET any inherited user-PII env (a contaminated parent may have exported it) so
# identity-guard.mjs (malice-guard) stays green regardless of how run.sh is invoked. Mirrors
# USER_PII_ENV_PATTERNS in skills/_shared/lib/identity-guard.mjs.
for piivar in $(env | cut -d= -f1 | grep -iE 'GOOGLE_LOGIN|COMPOSIO|GCAL|GOOGLE_CALENDAR|GMAIL_REFRESH|GMAIL_TOKEN|USER.?GMAIL|TELEGRAM|^USER_|USER.?PHONE|USER.?CONTACT' 2>/dev/null); do
  unset "$piivar" 2>/dev/null || true
done
PKVAR="${PKVAR:-BLOCKRUN_WALLET_KEY}"
EARN_STATE_ROOT="${EARN_STATE_ROOT:-${LIFE_MANAGER_SKILLS_STATE_ROOT:-${ANICCA_HOME:-$HOME/.local/state/life-manager}/state/skills}/earn}"
LEDGER="${EARN_LEDGER:-$EARN_STATE_ROOT/earn-ledger.jsonl}"
mkdir -p "$EARN_STATE_ROOT"
WAKE="${WAKE_ID:-$(date -u +%s)}"
MODE="${EARN_MODE:-discover}"

# THIS instance's own EVM signing key -- file-gated on ANICCA_HOME (resolve-identity.mjs), NEVER the
# shared legacy environment BLOCKRUN_WALLET_KEY (that key belongs to another instance; using it made Franklin's
# earn slots sign with automaton's wallet). Mirrors economy/gig/run.sh:49-56 (per-instance, fail-closed).
unset ANICCA_EVM_PRIVATE_KEY 2>/dev/null || true   # an env override must not beat the ANICCA_HOME file
SIGNKEY=$(node "$HERE/lib/resolve-identity.mjs" evm 2>/dev/null)
if [ -z "$SIGNKEY" ]; then
  echo "[earn] no signing key resolved for this instance (ANICCA_HOME=${ANICCA_HOME:-unset}) -- HALT (fail-closed); never fall back to another instance's key."
  exit 0
fi
export "$PKVAR=$SIGNKEY"   # children (execute-swap.py/hl.py/execute-yield.mjs...) read the key via $PKVAR -- give them THIS instance's
wallet_addr() {
  # node+viem, NOT python/eth_account: under launchd's clean PATH python3 has no eth_account on
  # sys.path, so this silently returned '' and a free Franklin's x402 wake HALTed with wallet=''
  # despite a resolvable key (x402-zero-to-one 2026-07-14). node+viem is always present (seller dep).
  node "$HERE/lib/resolve-identity.mjs" evm-address 2>/dev/null || echo ""
}
W="$(wallet_addr)"
WLOW="$(echo "$W" | tr 'A-F' 'a-f')"

record_line() { # $1 = json — defense-in-depth: strip PII env even if parent unset loop missed it
  env -u GOOGLE_LOGIN_PASSWORD -u GOOGLE_LOGIN_EMAIL \
      -u COMPOSIO_API_KEY -u COMPOSIO_AUTH_TOKEN \
      node "$HERE/lib/record.mjs" "$1" "$LEDGER"
}

# distribute_ubi: after a PROFITABLE external wake, send a share of THIS wake's net to AI+human
# recipients (own wallet only). Fail-soft: never bricks the wake (the earn already succeeded).
# $1 = the SAME earn-line JSON we just recorded PROFITABLE.
distribute_ubi() {
  UBI_OUT=$(node "$HERE/../ubi/distribute-ubi.mjs" "$1" 2>/dev/null || true)
  echo "[earn] ubi -> ${UBI_OUT:-noop}"
}

# P1 (spec §3/§4) fail-closed cumulative-net guard — the ONE-LINE integration pattern for
# every earn skill's pass boundary (mirrors polymarket-trade/run.sh's existing KILL-switch
# idiom: a single check at the very top, before doing anything this wake). "" for source =
# check the per-agent (wallet-wide) scope only — this wake hasn't picked its source yet.
#
# FIND-A fix (adversary round 2): ALWAYS call the guard, even when $WLOW is empty (identity
# resolution failed, the #27 broken-identity class). The old `[ -n "$WLOW" ] && ...` short-
# circuited the whole check away whenever WLOW was empty, so a broken-identity wake proceeded
# and recorded its loss under the literal wallet string "unknown" (`${WLOW:-unknown}` below) —
# invisible to every real-wallet-scoped check forever. earn-guard.mjs's CLI itself now
# fail-closed HALTs on an empty wallet (exit 1), so calling it unconditionally is correct: a
# broken identity is exactly when we must NOT proceed, never a reason to bypass the gate.
# x402 exemption (x402-zero-to-one 2026-07-14): strategy=x402 deploys ZERO capital — it only keeps
# the paid HTTP shop open, USDC can only flow IN. Blocking it on a cumulative-net breach is a
# deadlock: a broke instance's only risk-free way back to positive is exactly this slot (observed
# live: founder wake HALTed at cumulativeNet=-0.009 while holding a Bazaar-listed shop). Identity
# stays fail-closed — the SIGNKEY gate above already HALTed if this instance has no resolvable key.
if [ "${EARN_STRATEGY:-}" = "x402" ] && [ -n "$WLOW" ]; then
  echo "[earn] P1 guard: x402 (zero-capital) exempt from cumulative-net halt — shop stays open."
elif ! node "$HERE/../_shared/lib/earn-guard.mjs" check "$WLOW" "" "$LEDGER"; then
  echo "[earn] P1 GUARD: cumulative net breach or unresolved wallet (wallet='$WLOW') — HALT (fail-closed), skipping wake."
  exit 0
fi

if [ "$MODE" = "discover" ]; then
  # Discovery wake: the agent found candidates but executed nothing on-chain yet.
  # Record a narrate line so the ledger shows the wake happened. NEVER counts as GATE-0.
  SRC="${EARN_SOURCE:-x402}"
  JSON=$(python3 -c "import json,sys; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'$SRC','task':'discover','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
  OUT=$(record_line "$JSON")
  echo "[earn] discover wake=$WAKE source=$SRC -> $OUT"
  exit 0
fi

# execute mode.
# GATE-0 default = EXTERNAL revenue (0xwork). Swap is demoted to a non-gate runway fallback ONLY:
# a swap is net-zero asset rotation (Anicca's own ETH -> its own USDC), so the classifier
# (lib/ledger.mjs isProfitable) now rejects it. No EARN_STRATEGY value can mint GATE-0 from a swap.
STRATEGY="${EARN_STRATEGY:-yield}"

# SANITIZED model args. The old inline ANICCA_ARGS default with a literal {} MIS-PARSES in bash: the
# brace default inside the expansion closes at the first `}`, so a real {"action":"close"} became
# {"action":"close"}} (invalid JSON) → every parse fell to its default → the model's coin/side/size/
# action/launch decisions were ALL discarded (the HL close never fired: action='' != 'close', 2026-06-22).
# Sanitize ONCE here; AARGS is verbatim when set, {} when not — every parse below reads "$AARGS".
AARGS="${ANICCA_ARGS:-}"; [ -z "$AARGS" ] && AARGS='{}'

# --- strategy=0xwork: REAL EXTERNAL REVENUE (a poster's escrow pays USDC to our wallet) -------
if [ "$STRATEGY" = "0xwork" ] && [ -z "${EARN_TX:-}" ]; then
  RES=$(OXWORK_PKVAR="$PKVAR" PKVAR="$PKVAR" python3 "$HERE/execute-0xwork.py" 2>/dev/null)
  echo "[earn] 0xwork result: $RES"
  PAYTX=$(printf '%s' "$RES" | python3 -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get('payout_tx','') or '')" 2>/dev/null)
  TASKID=$(printf '%s' "$RES" | python3 -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get('task_id','') or 'claimed-awaiting-approval')" 2>/dev/null)
  if [ -z "$PAYTX" ]; then
    # No external payout this wake (no task / claimed+submitted awaiting poster approval).
    # Record NARRATE — never GATE-0 (no external:true). Honest residual.
    JSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'0xwork','task':'$TASKID','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
    OUT=$(record_line "$JSON")
    echo "[earn] 0xwork narrate (no external payout yet) -> $OUT"
    exit 0
  fi
  # Assert the payout is an EXTERNAL inbound USDC transfer (from=taskPool) BEFORE recording GATE-0.
  RECEIPT=$(curl -s "${BASE_RPC_URL:-https://mainnet.base.org}" -H 'content-type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getTransactionReceipt\",\"params\":[\"$PAYTX\"]}")
  EXT=$(printf '%s' "$RECEIPT" | node -e "let s='';process.stdin.on('data',d=>s+=d);process.stdin.on('end',async()=>{try{const r=JSON.parse(s).result;const o=await import('$HERE/lib/oxwork.mjs');console.log(await o.isExternalPayout(r,'$WLOW'))}catch(e){console.log('false')}})")
  if [ "$EXT" != "true" ]; then
    echo "[earn] REJECT: $PAYTX is not an external 0xwork payout (from!=taskPool or no USDC transfer to us) — NOT GATE-0"
    exit 1
  fi
  STATUS=$(node -e "import('$HERE/../_shared/lib/verify-tx.mjs').then(m=>m.receiptStatus('$PAYTX')).then(s=>console.log(s||'null')).catch(()=>console.log('null'))")
  BEFORE=$(printf '%s' "$RES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('before_usdc',0))" 2>/dev/null)
  AFTER=$(node -e "import('$HERE/../_shared/lib/usdc.mjs').then(m=>m.usdcBalance('$WLOW')).then(b=>console.log(b)).catch(()=>console.log('$BEFORE'))")
  AMT=$(node -e "console.log(Math.max(0,($AFTER)-($BEFORE)))")
  # external:true is what unlocks isProfitable() — set ONLY here, after the assertion passed.
  JSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'0xwork','task':'$TASKID','earn_usdc':float('$AMT'),'cost_usdc':0,'tx':'$PAYTX','status':'$STATUS','external':True,'wake':'$WAKE'}))")
  OUT=$(record_line "$JSON")
  echo "[earn] 0xwork recorded -> $OUT"
  if [ "$OUT" = "PROFITABLE" ]; then
    echo "[earn] GATE-0 MET: external revenue wake recorded (net>0, status 0x1, external inbound)."
    distribute_ubi "$JSON"
    exit 0
  fi
  echo "[earn] 0xwork wake recorded but NOT profitable (status=$STATUS). Not GATE-0."
  exit 0
fi

# --- strategy=hl: LLM-decided Hyperliquid perp (HARD RULE #0: the model decides coin/side/size) ----
# The model passes its decision in ANICCA_ARGS, e.g. {"strategy":"hl","coin":"ETH","side":"long",
# "size_usd":20,"sl_pct":3,"tp_pct":6}. hl.py runs in its own venv (deps isolated). Funding = USDC on
# the Hyperliquid account (Arbitrum bridge). If the HL account is unfunded or args are missing, hl.py
# returns an error/usage which we record as a NARRATE line (never bricks the wake).
if [ "$STRATEGY" = "hl" ] && [ -z "${EARN_TX:-}" ]; then
  HLDIR="$HERE/hl-trade"
  # Ensure an isolated venv with the HL deps EXISTS wherever this runs (local body, mother, or cloud).
  # venvs aren't relocatable, so build it in-place on first use rather than rely on a synced one.
  if [ ! -x "$HLDIR/.venv/bin/python" ]; then
    python3 -m venv "$HLDIR/.venv" >/dev/null 2>&1 \
      && "$HLDIR/.venv/bin/pip" install -q hyperliquid-python-sdk eth_account >/dev/null 2>&1 || true
  fi
  HLPY="$HLDIR/.venv/bin/python"; [ -x "$HLPY" ] || HLPY="python3"

  # REQ-E3: reconcile fill-based realized P&L on EVERY wake that reaches this branch, BEFORE the
  # anti-churn cooldown gate and BEFORE branching on ACTION=close/new-position open — this is
  # what makes an exchange-side auto-close (take-profit/stop-loss/liquidation) get recorded even
  # when the model issues no explicit close this wake (behavioral-spec.md REQ-A2/REQ-B*).
  RECON=$(PKVAR="$PKVAR" "$HLPY" "$HLDIR/hl.py" reconcile --wake "$WAKE" 2>&1)
  echo "[earn] hl reconcile -> $RECON"

  COIN=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('coin','ETH'))" 2>/dev/null || echo ETH)
  SIDE=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('side','') or '')" 2>/dev/null)
  SIZE=$(printf '%s' "$AARGS" | python3 -c "import json,sys;d=json.load(sys.stdin) or {};print(d.get('size_usd','') or '')" 2>/dev/null)
  ACTION=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('action','') or '')" 2>/dev/null)

  # Read the live account/positions FIRST so the model can MANAGE what it has open (not just open new).
  ACC=$(PKVAR="$PKVAR" "$HLPY" "$HLDIR/hl.py" account 2>/dev/null)
  POS=$(printf '%s' "$ACC" | python3 -c "import json,sys
try:
 d=json.load(sys.stdin) or {}; p=d.get('open_positions') or []
 print('|'.join(f\"{x.get('coin')} sz={x.get('szi')} entry={x.get('entry')} uPnL={x.get('uPnL')}\" for x in p))
except Exception: print('')" 2>/dev/null)

  # #16 ANTI-CHURN GUARD (money-safety rate-limit, NOT a trade decision — same category as the loop-detect
  # cooldown): automaton thrashed HL ~138 fills/day, opening+closing tiny ETH longs minutes apart so fees
  # ate the edge and net ~flat. Rate-limit BRAIN-driven open/close to >= HL_COOLDOWN_MIN apart so a thesis
  # can develop and fees don't churn the account. The exchange-side stop/take (set on open) still
  # auto-closes regardless — this only blocks whim re-trades, never a real SL/TP exit.
  HL_LAST="$HLDIR/.last-trade-ts"; HL_COOLDOWN_MIN="${HL_COOLDOWN_MIN:-60}"
  _hl_now=$(date +%s); _hl_last=$(cat "$HL_LAST" 2>/dev/null || echo 0)
  # #16 robustness (adversary a604b23): a corrupt/partial .last-trade-ts or a non-numeric HL_COOLDOWN_MIN
  # must NEVER brick the wake. Under `set -u` a non-numeric value in $(( )) is a FATAL "unbound variable"
  # that aborts the whole HL block silently (exit 0). Coerce both to safe integers first.
  case "$_hl_last" in ''|*[!0-9]*) _hl_last=0 ;; esac
  case "$HL_COOLDOWN_MIN" in ''|*[!0-9]*) HL_COOLDOWN_MIN=60 ;; esac
  _hl_since=$(( (_hl_now - _hl_last) / 60 ))
  if { [ "$ACTION" = "close" ] || { [ -n "$SIDE" ] && [ -n "$SIZE" ]; }; } && [ "$_hl_since" -lt "$HL_COOLDOWN_MIN" ]; then
    echo "[earn] hl anti-churn: last trade ${_hl_since}min ago < ${HL_COOLDOWN_MIN}min → HOLD (no churn); exchange SL/TP still active"
    TASK="hl-cooldown — holding ${POS:-flat} (${_hl_since}min since last trade, min ${HL_COOLDOWN_MIN})"
    JSON=$(python3 -c "import json,sys; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'hl-trade','task':sys.argv[1][:160],'earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))" "$TASK" 2>/dev/null)
    OUT=$(record_line "$JSON"); echo "[earn] hl cooldown -> $OUT"; exit 0
  fi

  # MANAGE: the model decided to close (action=close), OR a stop/take got hit — realise the position.
  # REQ-A3 (F-1 fix): this branch NEVER records its own earn_usdc/cost_usdc ledger line for the
  # close. reconcile() (hl.py reconcile, invoked above per REQ-E3) is the SOLE recorder of HL
  # realized P&L — it will pick up this close's own settled fill(s) on this or a later wake, once
  # userFills reflects them. `hl.py close`'s JSON no longer carries a pre-close pnl field (REQ-A1)
  # to read here, and recording a value of our own would either be a fabricated zero or a
  # duplicate of what the reconciler already records.
  if [ "$ACTION" = "close" ] && [ -n "$POS" ]; then
    RES=$(PKVAR="$PKVAR" "$HLPY" "$HLDIR/hl.py" close "$COIN" 2>&1)
    date +%s > "$HL_LAST" 2>/dev/null || true   # #16: stamp the trade time for the anti-churn cooldown
    echo "[earn] hl close $COIN -> $RES"
    exit 0
  fi

  if [ -z "$SIDE" ] || [ -z "$SIZE" ]; then
    # No new trade → surface the OPEN position + PnL so the model can decide next wake (hold/close).
    echo "[earn] hl positions: ${POS:-none}"
    TASK="hl-observe${POS:+ — OPEN: $POS (pass action:close to realise, or hold)}"
    JSON=$(python3 -c "import json,sys; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'hl-trade','task':sys.argv[1][:160],'earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))" "$TASK" 2>/dev/null)
    OUT=$(record_line "$JSON"); echo "[earn] hl narrate -> $OUT"; exit 0
  fi
  SL=$(printf '%s' "$AARGS" | python3 -c "import json,sys;d=json.load(sys.stdin) or {};print(d.get('sl_pct','') or '')" 2>/dev/null)
  TP=$(printf '%s' "$AARGS" | python3 -c "import json,sys;d=json.load(sys.stdin) or {};print(d.get('tp_pct','') or '')" 2>/dev/null)
  # FUND-HL: if the HL account can't cover this trade, fund it from Base USDC (relay). fund-hl.mjs has an
  # economic guard — it REFUSES to bridge when the ~fixed ~$1.2 fee would be a high % of the amount, so a
  # too-small wallet just gets a "needs more capital" note instead of burning money. Anicca funds itself.
  HLBAL=$(PKVAR="$PKVAR" "$HLPY" "$HLDIR/hl.py" account 2>/dev/null | python3 -c "import json,sys
try: print(float((json.load(sys.stdin) or {}).get('withdrawable',0) or 0))
except Exception: print(0)" 2>/dev/null || echo 0)
  if python3 -c "import sys;sys.exit(0 if float('$HLBAL')<float('$SIZE') else 1)" 2>/dev/null; then
    echo "[earn] hl balance \$$HLBAL < trade \$$SIZE → attempting self-fund (relay Base→HL, economic-guarded)"
    FUND=$(FUND_HL_USDC="$SIZE" PKVAR="$PKVAR" node "$HLDIR/fund-hl.mjs" 2>&1 | tail -1)
    echo "[earn] fund-hl: $FUND"
    if printf '%s' "$FUND" | grep -q '"funded": *false'; then
      JSON=$(python3 -c "import json,sys; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'hl-trade','task':'hl-fund-skipped: '+json.loads('''$FUND''').get('reason','?'),'earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))" 2>/dev/null)
      OUT=$(record_line "$JSON"); echo "[earn] hl fund skipped -> $OUT"; exit 0
    fi
  fi
  ARGS=(open "$COIN" "$SIDE" "$SIZE"); [ -n "$SL" ] && ARGS+=(--sl "$SL"); [ -n "$TP" ] && ARGS+=(--tp "$TP")
  RES=$(PKVAR="$PKVAR" "$HLPY" "$HLDIR/hl.py" "${ARGS[@]}" 2>&1)
  date +%s > "$HL_LAST" 2>/dev/null || true   # #16: stamp the trade time for the anti-churn cooldown
  echo "[earn] hl open result: $RES"
  JSON=$(python3 -c "import json,sys; r=json.loads('''$RES''') if '''$RES'''.strip().startswith('{') else {}; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'hl-trade','task':'hl $SIDE $COIN \$$SIZE','earn_usdc':0,'cost_usdc':0,'tx':r.get('oid','') or r.get('status',''),'wake':'$WAKE'}))" 2>/dev/null || python3 -c "import json;print(json.dumps({'wallet':'${WLOW:-unknown}','source':'hl-trade','task':'hl-open','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
  OUT=$(record_line "$JSON"); echo "[earn] hl recorded -> $OUT"; exit 0
fi

# --- strategy=x402: the x402 PRODUCT server + its whole listing lifecycle (passive earner) --------
# x402-sell/serve-v2.mjs is a persistent HTTP server (402 Payment Required → USDC to our wallet).
# ANICCA_ARGS.action dispatches this ONE loop-menu slot to 4 sub-behaviors (SELF-STORE-1,
# 2026-07-18 — deliberately NOT 4 separate registry slots, menu-size discipline):
#   ensure (default) — open/keep-alive the shop, then idempotently (re)list it on x402scan.
#   review           — aggregate sales/attempts logs: what sold, was any buyer external, or is
#                       this a demand problem? Read-only, no side effects.
#   improve          — combine market gaps + per-route sales bandit into one recommendation.
#   update           — force a re-listing after the model edited the product catalog.
# "store is up" = the instance's OWN port answers /.well-known/x402.json, NO MATTER which launchd
# label owns it (loop-made sellers historically port-fought the hand-made per-instance ones —
# never spawn a second seller when the port is already alive).
if [ "$STRATEGY" = "x402" ] && [ -z "${EARN_TX:-}" ]; then
  X402DIR="$HERE/x402-sell"
  ACTION=$(printf '%s' "$AARGS" | python3 -c "import json,sys
try: d=json.load(sys.stdin) or {}
except Exception: d={}
print(d.get('action') or 'ensure')" 2>/dev/null || echo ensure)

  # This instance's own port + hand-made per-instance launchd label, resolved from ANICCA_HOME
  # (serve-franklin1-boot.sh=8414, serve-franklin2-boot.sh=8413, serve-claude-p-boot.sh=8412).
  # X402_PORT always wins when the caller sets it explicitly.
  case "${ANICCA_HOME:-}" in
    *".franklin2-home"*) X402_INSTANCE="franklin2"; X402_DEFAULT_PORT=8413 ;;
    *".blockrun") X402_INSTANCE="franklin1"; X402_DEFAULT_PORT=8414 ;;
    *".anicca-founder") X402_INSTANCE="claude-p"; X402_DEFAULT_PORT=8412 ;;
    # :8403 is held by the OLD spec-09 echo endpoint (ai.anicca.x402-endpoint launchd); unresolved
    # instances fall back to 8404 (system bug found 2026-06-22).
    *) X402_INSTANCE=""; X402_DEFAULT_PORT=8404 ;;
  esac
  XPORT="${X402_PORT:-$X402_DEFAULT_PORT}"

  # FIVE-MINUTE REVENUE CONTROLLER: evaluate the bounded /llm offer experiment on EVERY x402
  # wake, before a model-selected review/update/ensure branch can exit. The model still chooses
  # the broader store action; this deterministic safety/control plane only rotates the active
  # price after five revenue-free minutes or freezes a proven winner. `store-improve.mjs` is
  # idempotent inside the interval, and store activation only runs when a variant changed.
  IMPROVE_RES=$(X402_PAYTO="${X402_PAYTO:-$W}" node "$X402DIR/store-improve.mjs" 2>/dev/null)
  EXP_ACTION=$(printf '%s' "$IMPROVE_RES" | python3 -c "import json,sys
try: print((json.load(sys.stdin).get('experiment') or {}).get('action') or '')
except Exception: print('')" 2>/dev/null)
  if [ "$EXP_ACTION" = "applied" ]; then
    ACT=$(X402_PAYTO="${X402_PAYTO:-$W}" X402_PUBLIC_URL="${X402_PUBLIC_URL:-}" node "$X402DIR/store-activate.mjs" 2>/dev/null)
    IMPROVE_RES=$(python3 -c "import json,sys
try: base=json.loads(sys.argv[1]); activation=json.loads(sys.argv[2])
except Exception: print(sys.argv[1]); raise SystemExit
base['activation']=activation
print(json.dumps(base,separators=(',',':')))" "$IMPROVE_RES" "${ACT:-{}}" 2>/dev/null || printf '%s' "$IMPROVE_RES")
  fi
  echo "[earn] x402 five-minute controller: ${IMPROVE_RES:-{}}"

  if [ "$ACTION" = "review" ]; then
    RES=$(X402_PAYTO="${X402_PAYTO:-$W}" node "$X402DIR/store-review.mjs" 2>/dev/null)
    echo "[earn] x402 review: $RES"
    JSON=$(python3 -c "import json,sys; print(json.dumps({'wallet':sys.argv[1],'source':'x402-review','task':sys.argv[2],'earn_usdc':0,'cost_usdc':0,'wake':sys.argv[3]}))" "${WLOW:-unknown}" "x402 review: ${RES:-{}}" "$WAKE" 2>/dev/null)
    OUT=$(record_line "$JSON"); echo "[earn] x402 review recorded -> $OUT"
    exit 0
  fi

  if [ "$ACTION" = "improve" ]; then
    RES="$IMPROVE_RES"
    echo "[earn] x402 improve: $RES"
    JSON=$(python3 -c "import json,sys; print(json.dumps({'wallet':sys.argv[1],'source':'x402-improve','task':sys.argv[2],'earn_usdc':0,'cost_usdc':0,'wake':sys.argv[3]}))" "${WLOW:-unknown}" "x402 improve: ${RES:-{}}" "$WAKE" 2>/dev/null)
    OUT=$(record_line "$JSON"); echo "[earn] x402 improve recorded -> $OUT"
    exit 0
  fi

  if [ "$ACTION" = "update" ]; then
    RES=$(X402_PAYTO="${X402_PAYTO:-$W}" X402_PUBLIC_URL="${X402_PUBLIC_URL:-}" node "$X402DIR/store-update.mjs" 2>/dev/null)
    echo "[earn] x402 update: $RES"
    JSON=$(python3 -c "import json,sys; print(json.dumps({'wallet':sys.argv[1],'source':'x402-update','task':sys.argv[2],'earn_usdc':0,'cost_usdc':0,'wake':sys.argv[3]}))" "${WLOW:-unknown}" "x402 update: ${RES:-{}}" "$WAKE" 2>/dev/null)
    OUT=$(record_line "$JSON"); echo "[earn] x402 update recorded -> $OUT"
    exit 0
  fi

  # ACTION=ensure (default, also legacy empty-args callers): open/keep-alive the shop, then register.
  if ! curl -sf "http://127.0.0.1:$XPORT/.well-known/x402.json" >/dev/null 2>&1; then
    # The seller must OUTLIVE this wake: a plain `nohup ... &` child dies with the wake's process
    # group (run-skill.mjs execFile kills the group on timeout/next-wake), which is why sellers
    # never persisted (observed live 2026-07-14: booted during the wake, DOWN minutes later).
    X402_LABEL="ai.anicca.x402-$X402_INSTANCE"
    if [ -n "$X402_INSTANCE" ] \
      && launchctl print "gui/$(id -u)/$X402_LABEL" >/dev/null 2>&1; then
      # launchd's per-user namespace is the authority. Agent instances may override HOME, so a
      # plist path under $HOME cannot prove that the host-level supervisor is absent.
      echo "[earn] x402 port $XPORT down — kickstarting existing job $X402_LABEL"
      launchctl kickstart -k "gui/$(id -u)/$X402_LABEL" 2>/dev/null || true
      sleep 3
    elif command -v launchctl >/dev/null 2>&1; then
      # No hand-made job for this instance/port yet: register a loop-owned launchd KeepAlive
      # service (same pattern as serve-mainnet-boot.sh) — survives wake boundaries AND loop
      # restarts, zero human. seller-boot-v2.sh execs serve-v2.mjs (v2 protocol, matches every
      # hand-made boot script). Elsewhere (cloud Linux), fall back to setsid detach.
      SLABEL="ai.anicca.x402-seller-$XPORT"
      SPLIST="$HOME/Library/LaunchAgents/$SLABEL.plist"
      mkdir -p "$HOME/Library/LaunchAgents"
      cat > "$SPLIST" <<SELLERPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$SLABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>$HERE/x402-sell/seller-boot-v2.sh</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>X402_PAYTO</key><string>$W</string>
    <key>X402_PORT</key><string>$XPORT</string>
    <key>X402_PUBLIC_URL</key><string>${X402_PUBLIC_URL:-}</string>
    <key>LIFE_MANAGER_ENV_FILE</key><string>${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/x402-seller-$XPORT.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/x402-seller-$XPORT.err.log</string>
</dict></plist>
SELLERPLIST
      launchctl bootstrap "gui/$(id -u)" "$SPLIST" 2>/dev/null \
        || launchctl kickstart -k "gui/$(id -u)/$SLABEL" 2>/dev/null || true
    else
      set -a; . "${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}" 2>/dev/null || true; set +a
      X402_PAYTO="$W" X402_PORT="$XPORT" X402_PUBLIC_URL="${X402_PUBLIC_URL:-}" setsid nohup node "$X402DIR/serve-v2.mjs" >/dev/null 2>&1 < /dev/null &
    fi
    sleep 3
  fi
  UP=$(curl -sf "http://127.0.0.1:$XPORT/.well-known/x402.json" >/dev/null 2>&1 && echo up || echo down)
  echo "[earn] x402 server: $UP (payTo=$W port=$XPORT instance=${X402_INSTANCE:-unknown})"

  # FIND BUYERS pt.1: no explicit X402_PUBLIC_URL yet (e.g. no tsnet/funnel for this instance) —
  # fall back to a cloudflared tunnel so the store is still discoverable. URL persists in a state
  # file; we only re-tunnel when it's missing.
  STATEDIR="$EARN_STATE_ROOT"; mkdir -p "$STATEDIR"; URLFILE="$STATEDIR/x402-public-url.txt"
  if [ "$UP" = "up" ] && [ -z "${X402_PUBLIC_URL:-}" ] && command -v cloudflared >/dev/null 2>&1; then
    if ! pgrep -f "cloudflared.*localhost:$XPORT" >/dev/null 2>&1; then
      nohup cloudflared tunnel --no-autoupdate --url "http://localhost:$XPORT" >"$STATEDIR/x402-tunnel.log" 2>&1 &
      sleep 8
    fi
    PUB=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$STATEDIR/x402-tunnel.log" 2>/dev/null | tail -1)
    PREV=$(cat "$URLFILE" 2>/dev/null || echo "")
    if [ -n "$PUB" ] && [ "$PUB" != "$PREV" ]; then
      printf '%s' "$PUB" > "$URLFILE"
      X402_PUBLIC_URL="$PUB"
      # advertise to the colony forum (anicca finding its own buyers) — best-effort, never bricks.
      ADTITLE="x402 service: web-research brief for \$0.02 USDC"
      ADBODY="Anicca is selling a live web-research brief over x402. Pay \$0.02 USDC (Base) to GET ${PUB}/research?q=YOUR_QUERY and receive a markdown brief. payTo ${W}. Agents welcome."
      gh issue create -R "${ANICCA_FORUM_REPO:-Daisuke134/anicca}" -t "$ADTITLE" -b "$ADBODY" >/dev/null 2>&1 || true
      echo "[earn] x402 advertised public endpoint: $PUB"
    elif [ -n "$PUB" ]; then
      X402_PUBLIC_URL="$PUB"
    fi
  fi

  # FIND BUYERS pt.2: idempotently (re)list the store on x402scan (SIWX-registered origin). Only
  # actually re-signs when the catalog changed or the last registration is stale (store-ensure-
  # register.mjs's own decision) — a wake never re-registers an unchanged, freshly-listed shop.
  REG="{}"
  if [ -n "${X402_PUBLIC_URL:-}" ]; then
    REG=$(X402_PAYTO="${X402_PAYTO:-$W}" X402_PUBLIC_URL="$X402_PUBLIC_URL" node "$X402DIR/store-ensure-register.mjs" 2>/dev/null)
    echo "[earn] x402 register: $REG"
  fi

  X402_TASK="x402 server $UP"
  [ -n "${X402_PUBLIC_URL:-}" ] && X402_TASK="$X402_TASK public=$X402_PUBLIC_URL"
  JSON=$(python3 -c "
import json,sys
reg={}
try: reg=json.loads(sys.argv[4])
except Exception: pass
out={'wallet':sys.argv[1],'source':'x402-serve','task':sys.argv[2],'earn_usdc':0,'cost_usdc':0,'wake':sys.argv[3]}
out.update({k:v for k,v in reg.items() if k in ('registered','productCount','reregistered','reason')})
print(json.dumps(out))
" "${WLOW:-unknown}" "$X402_TASK" "$WAKE" "$REG" 2>/dev/null)
  OUT=$(record_line "$JSON"); echo "[earn] x402 narrate -> $OUT"; exit 0
fi

# --- strategy=token: launch / manage Anicca's own token (MoltX Launchpad) — model-gated -----------
# Costs ~$2.70 (0.001 ETH) + creates a REAL token, so it only acts when the model explicitly decides
# (ANICCA_ARGS {"strategy":"token","launch":true,"name":"...","symbol":"...","image":"<url>"}). Otherwise
# it reports the deposit address (read-only) and narrates. Token fees later fund compute.
if [ "$STRATEGY" = "token" ] && [ -z "${EARN_TX:-}" ]; then
  TLPY="python3"
  WANT=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print(str((json.load(sys.stdin) or {}).get('launch','')).lower())" 2>/dev/null)
  if [ "$WANT" = "true" ]; then
    NAME=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('name','ANICCA'))" 2>/dev/null)
    SYM=$(printf '%s' "$AARGS" | python3 -c "import json,sys;print((json.load(sys.stdin) or {}).get('symbol','ANICCA'))" 2>/dev/null)
    RES=$(PKVAR="$PKVAR" "$TLPY" "$HERE/token-launch/launchpad.py" deposit 2>&1)
    echo "[earn] token deposit/launch ($NAME/$SYM): $RES"
    JSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'token','task':'token-launch $SYM','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
  else
    echo "[earn] token: no launch decision (set args.launch=true to create)"
    JSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'token','task':'token-observe','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
  fi
  OUT=$(record_line "$JSON"); echo "[earn] token narrate -> $OUT"; exit 0
fi

# --- strategy=swap: run.sh performs a NET-ZERO asset rotation (runway fallback, NEVER GATE-0) -
if [ "$STRATEGY" = "swap" ] && [ -z "${EARN_TX:-}" ]; then
  RES=$(PKVAR="$PKVAR" python3 "$HERE/execute-swap.py" 2>/dev/null)
  echo "[earn] swap result: $RES"
  # abort/error (e.g. ETH below gas reserve) -> degrade to a discover narrate line, never brick.
  ERR=$(printf '%s' "$RES" | python3 -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get('error') or d.get('abort') or '')" 2>/dev/null)
  if [ -n "$ERR" ]; then
    SRC="swap-eth-usdc"
    JSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'$SRC','task':'swap-skipped:$ERR','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
    OUT=$(record_line "$JSON")
    echo "[earn] swap skipped ($ERR) -> recorded NARRATE -> $OUT"
    exit 0
  fi
  EARN_TX=$(printf '%s' "$RES" | python3 -c "import json,sys;print(json.load(sys.stdin)['tx'])")
  STATUS=$(printf '%s' "$RES" | python3 -c "import json,sys;print(json.load(sys.stdin)['status'])")
  EARN_AMOUNT=$(printf '%s' "$RES" | python3 -c "import json,sys;print(json.load(sys.stdin)['gross_usdc'])")
  COST=$(printf '%s' "$RES" | python3 -c "import json,sys;print(json.load(sys.stdin)['cost_usdc'])")
  EARN_SOURCE="swap-eth-usdc"
  EARN_TASK="${EARN_TASK:-eth->usdc liquidation for compute runway}"
  echo "[earn] tx=$EARN_TX status=$STATUS source=$EARN_SOURCE gross=$EARN_AMOUNT cost=$COST"
  JSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'$EARN_SOURCE','task':'''$EARN_TASK''','earn_usdc':float('$EARN_AMOUNT'),'cost_usdc':float('$COST'),'tx':'$EARN_TX','status':'$STATUS','wake':'$WAKE'}))")
  OUT=$(record_line "$JSON")
  echo "[earn] recorded -> $OUT"
  # A swap line carries NO 'external' flag, so isProfitable() returns false by construction.
  # OUT is always NARRATE here: a swap is asset rotation (runway top-up), NEVER GATE-0.
  echo "[earn] swap wake recorded as NARRATE (asset rotation, not external revenue). Not GATE-0."
  exit 0
fi

# --- GAS FLOOR: never gas-stall. A wallet with USDC but zero native ETH cannot send ANY tx. Before
# the tx-doing legs, ensure a minimum ETH balance (self-restored by unwrapping a little WETH, or
# swapping a bit of USDC) so every Anicca can always act.
if [ "$STRATEGY" = "yield" ] && [ -z "${EARN_TX:-}" ]; then
  GRES=$(PKVAR="$PKVAR" node "$HERE/ensure-gas.mjs" 2>/dev/null)
  echo "[earn] gas check: $GRES"
fi

# --- INVESTING leg — DISABLED by default (2026-06-21). It ran a USDC->ETH DCA swap IN THE SAME WAKE
# right before the yield deposit; the swap changed the USDC balance / occupied the nonce, so the yield
# deposit that followed REVERTED (status 0x0, burning gas) while a yield run ALONE succeeds. It is also
# speculative (ETH can fall) — not "principal-preserving". The model can still invest explicitly via
# EARN_STRATEGY=invest. Set EARN_INVEST_LEG=1 to re-enable the auto leg.
if [ "$STRATEGY" = "yield" ] && [ "${EARN_INVEST_LEG:-0}" = "1" ] && [ -z "${EARN_TX:-}" ]; then
  IRES=$(PKVAR="$PKVAR" node "$HERE/execute-invest.mjs" 2>/dev/null)
  echo "[earn] invest result: $IRES"
  IKIND=$(printf '%s' "$IRES" | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('kind',''))
except Exception: print('')" 2>/dev/null)
  if [ "$IKIND" = "invest" ]; then
    ITX=$(printf '%s' "$IRES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('tx',''))" 2>/dev/null)
    IAMT=$(printf '%s' "$IRES" | python3 -c "import json,sys;print(json.load(sys.stdin).get('bought_usd',0))" 2>/dev/null)
    IJSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'invest-eth-dca','task':'dca_buy_eth_${IAMT}','earn_usdc':0,'cost_usdc':0,'tx':'$ITX','kind':'invest','wake':'$WAKE'}))")
    record_line "$IJSON" >/dev/null 2>&1 || true
    echo "[earn] invest dca_buy \$$IAMT ETH (blue-chip leg) recorded"
  fi
fi

# --- strategy=yield: GOAT earner — deploy idle USDC into DeFi yield (Aave v3) ---------------
# The agent's reliable, always-available earner. Net worth grows via accrual (aUSDC balance),
# withdrawable any time. NOT external revenue (kind:yield, external:false) -> never GATE-0;
# it is honest capital deployment at the market lending rate. When other earners have no live
# opportunity, the loop falls back here so a wake always does something productive.
if [ "$STRATEGY" = "yield" ] && [ -z "${EARN_TX:-}" ]; then
  RES=$(PKVAR="$PKVAR" node "$HERE/execute-yield.mjs" 2>/dev/null)
  echo "[earn] yield result: $RES"
  ABORT=$(printf '%s' "$RES" | python3 -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
print(d.get('abort') or d.get('error') or '')" 2>/dev/null)
  if [ -n "$ABORT" ]; then
    JSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'yield-aave-v3','task':'yield-skipped:$ABORT','earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))")
    OUT=$(record_line "$JSON")
    echo "[earn] yield skipped ($ABORT) -> NARRATE -> $OUT"
    exit 0
  fi
  # Record the ACTUAL result execute-yield returned — never a hardcoded "deploy" line. A hold (no tx)
  # was being logged as a fake "deploy idle USDC to Aave v3" with tx='' (misleading ledger). Now the
  # source/task/tx mirror what really happened: deploy <protocol> tx / refill <protocol> tx / hold (no tx).
  JSON=$(printf '%s' "$RES" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: d={}
kind=d.get('kind'); action=d.get('action'); proto=d.get('protocol','')
W='${WLOW:-unknown}'; WAKE='$WAKE'
if kind=='yield_hold' or action=='hold':
    print(json.dumps({'wallet':W,'source':'yield','task':'hold (buffer healthy, position accruing)','kind':'yield_hold','earn_usdc':0,'cost_usdc':0,'liquid_usdc':d.get('liquid_usdc',0),'wake':WAKE}))
else:
    src='yield-'+(proto.split(':')[0] if proto else 'defi'); src='yield-beefy-morpho' if 'morpho' in proto else ('yield-aave-v3' if 'aave' in proto else src)
    amt=d.get('deposited_usdc') or d.get('refilled_usdc') or 0
    print(json.dumps({'wallet':W,'source':src,'task':(action or 'deploy')+' '+proto,'kind':'yield','deposited_usdc':float(amt),'earn_usdc':0,'cost_usdc':0,'tx':d.get('tx',''),'status':d.get('status',''),'external':False,'wake':WAKE}))
" 2>/dev/null)
  OUT=$(record_line "$JSON")
  LIFE_MANAGER_REPO="${ANICCA_REPO:-$HERE/../..}" \
    LM_FINANCIAL_RECORDS_DIR="${LM_FINANCIAL_RECORDS_DIR:-${CFO_STATE_DIR:-$HOME/.local/state/life-manager/life-manager-cfo-hourly}/financial-records}" \
    LM_CFO_SUBJECT_ID="${LM_CFO_SUBJECT_ID:-${LM_UID:-local}}" \
    node "${ANICCA_REPO:-$HERE/../..}/apps/life-manager/scripts/record-yield-financial.js" "$RES" >/dev/null
  echo "[earn] yield $(printf '%s' "$RES" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('action') or d.get('kind'),d.get('tx','') or '')" 2>/dev/null) -> $OUT"
  exit 0
fi

# --- externally-executed earn (e.g. x402 inbound): an on-chain earn already happened ---------
: "${EARN_TX:?execute mode needs EARN_TX (the receipt hash) unless EARN_STRATEGY=swap}"
: "${EARN_SOURCE:?execute mode needs EARN_SOURCE}"
: "${EARN_AMOUNT:?execute mode needs EARN_AMOUNT (gross USDC earned)}"
COST="${EARN_COST:-0}"

# 1) on-chain receipt status (0x1 = success).
STATUS=$(node -e "import('$HERE/../_shared/lib/verify-tx.mjs').then(m=>m.receiptStatus(process.argv[1])).then(s=>console.log(s===null?'null':s)).catch(e=>{console.error(e.message);process.exit(1)})" "$EARN_TX")
echo "[earn] tx=$EARN_TX status=$STATUS source=$EARN_SOURCE"

# 2) record the line (record.mjs derives net + classifies profitable).
JSON=$(python3 -c "import json; print(json.dumps({'wallet':'${WLOW:-unknown}','source':'$EARN_SOURCE','task':'${EARN_TASK:-earn}','earn_usdc':float('$EARN_AMOUNT'),'cost_usdc':float('$COST'),'tx':'$EARN_TX','status':'$STATUS','wake':'$WAKE'}))")
OUT=$(record_line "$JSON")
echo "[earn] recorded -> $OUT"

# 3) GATE-0: only a confirmed, net-positive wake is a real launch gate.
if [ "$OUT" = "PROFITABLE" ]; then
  echo "[earn] GATE-0 MET: profitable wake recorded (net>0, status 0x1)."
  distribute_ubi "$JSON"
  exit 0
fi
echo "[earn] wake recorded but NOT profitable (status=$STATUS). Not GATE-0."
exit 0
