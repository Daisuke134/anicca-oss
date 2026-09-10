#!/bin/bash
# sol-trade run.sh — THIN HARNESS. THIS FILE DECIDES NOTHING. (spec §0.25)
# The base agent (BlockRunAI/Franklin-Trading, `franklin-trading` CLI) does its OWN
# research / sizing / execution and pays for its OWN model calls via x402 from its
# OWN Solana wallet (8Fpqd…). Wrapper = kill-switch → one real pass → trace line.
set -u
# FIND-001 (money-safety): an ambient ANICCA_SOLANA_PRIVATE_KEY would otherwise beat the
# ANICCA_HOME-gated file resolution on BOTH derivations below (resolveSolanaSecret's FIRST
# check, resolve-identity.mjs:118-120), making OWN_WALLET==CLI_WALLET for ANY instance and
# bypassing the identity-match guard entirely. Unset it before deriving any Solana address --
# mirrors earn/run.sh's `unset ANICCA_EVM_PRIVATE_KEY` (REQ-001). franklin-trading itself reads
# ~/.blockrun directly and never needs this var, so unsetting it here is safe.
unset ANICCA_SOLANA_PRIVATE_KEY 2>/dev/null || true
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${SOL_TRADE_STATE_ROOT:-${EARN_STATE_ROOT:-${LIFE_MANAGER_SKILLS_STATE_ROOT:-${ANICCA_HOME:-$HOME/.local/state/life-manager}/state/skills}/earn}}"; mkdir -p "$STATE_DIR"
TRACE="$STATE_DIR/sol-trade.trace.jsonl"
FT_MODEL="${SOL_TRADE_MODEL:-openai/gpt-5-mini}"   # cheapest WORKING tool-caller (~pennies/session), don't bleed the bankroll (FIX-C)

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

if [ -f "$SKILL_DIR/KILL" ]; then
  echo "{\"ts\":\"$(now)\",\"slot\":\"earn/sol-trade\",\"action\":\"skip\",\"reason\":\"kill-switch\"}" >> "$TRACE"
  exit 0
fi

command -v franklin-trading >/dev/null || { echo "franklin-trading CLI missing" >&2; exit 1; }

# IDENTITY-MATCH GUARD (money-safety): the franklin-trading CLI hardcodes its wallet to ~/.blockrun,
# so ANY instance running this slot would trade Franklin's wallet. Only the instance that OWNS
# ~/.blockrun (Franklin) may proceed; every other instance (automaton) HALTs before touching the CLI.
# NOTE(2026-07-10 regression fix): the daemon rsyncs only skills/ into $ANICCA_HOME, NOT runtime/,
# so a $SKILL_DIR-relative path resolves to a non-existent ~/.blockrun/runtime/ in the deployed copy
# and made this guard skip EVERY wake since commit 3d97c59. Resolve via $ANICCA_REPO (daemon-exported,
# rsync-independent) instead; fall back to the repo-relative path for in-repo test runs.
WADDR="${ANICCA_REPO:-$SKILL_DIR/../../..}/runtime/wallet-address-solana.mjs"
OWN_WALLET=$(node "$WADDR" 2>/dev/null)
CLI_WALLET=$(ANICCA_HOME="$HOME/.blockrun" node "$WADDR" 2>/dev/null)
if [ -z "$OWN_WALLET" ] || [ -z "$CLI_WALLET" ] || [ "$OWN_WALLET" != "$CLI_WALLET" ]; then
  echo "{\"ts\":\"$(now)\",\"slot\":\"earn/sol-trade\",\"action\":\"skip\",\"reason\":\"identity-mismatch (own=${OWN_WALLET:-none} cli=${CLI_WALLET:-none}); only Franklin(.blockrun) may run this slot\"}" >> "$TRACE"
  exit 0
fi

# cumulative-loss guard (fail-closed) — same one-line idiom as economy/gig/run.sh:62 / earn/run.sh
LEDGER="${EARN_LEDGER:-$STATE_DIR/earn-ledger.jsonl}"
if ! node "$SKILL_DIR/../../_shared/lib/earn-guard.mjs" check "$OWN_WALLET" "sol-trade" "$LEDGER" 2>/dev/null; then
  echo "{\"ts\":\"$(now)\",\"slot\":\"earn/sol-trade\",\"action\":\"skip\",\"reason\":\"earn-guard: cumulative net breach -- HALT (fail-closed)\"}" >> "$TRACE"
  exit 0
fi

# BASELINE STRATEGY (battle-tested seed — the AI starts here, then self-improves; #34/H8).
# Franklin's perp tools are PAPER, but JupiterSwap is a REAL on-chain Solana DEX swap — so REAL earning =
# disciplined spot round-trips: buy a token you have a clear TradingSignal edge on, take profit, swap back
# to USDC. The discipline (below) is the seed; the AI tunes it from its own P&L.
PROMPT="You are live with a REAL Solana wallet (this is your entire bankroll). First call your wallet/\
portfolio tool to see your exact USDC + SOL. BASELINE STRATEGY (start here, improve from your own results): \
1) A round-trip Jupiter swap costs ~0.4%+ in fees+slippage — so ONLY trade when TradingSignal gives a CLEAR \
bullish or bearish verdict with real conviction on a liquid token (SOL, major); if the signal is neutral/\
weak, DO NOT trade this session (holding USDC beats paying fees on noise). 2) When you do trade: size small, \
define your take-profit and stop BEFORE swapping, and swap back to USDC to realise. 3) Never swap more than \
you can afford to lose; keep enough SOL for gas. 4) You are running fully unsupervised on a timer — nobody \
reads this session or replies to it, so NEVER end your turn with a question, a menu of choices, or 'let me \
know if you want me to...'. If you catch yourself about to offer options, that means you already have \
enough information to decide right now — so decide. Execute a REAL swap only if the edge clears the fee \
hurdle; otherwise WAIT and say why in one line. Every pass ends in exactly one of those two states — a \
filled trade or a one-line WAIT reason — never an open question. Keep a note for next session. Mind model \
spend — your fuel is the same wallet. 5) EVERY tool call, including TradingSignal, is paid for via x402 \
from this same wallet, whether or not you end up trading — on a small bankroll this bleeds you quietly \
even while you correctly WAIT. Use judgment: if you checked TradingSignal very recently and nothing has \
plausibly moved enough to change the verdict, skip re-checking it this pass and just WAIT with a note — \
don't pay for the same answer twice in a row."

# --- SOL_GATE pre-gate (franklin-sol-evolvable-edge, REQ-001..018) ---------------------------
# Evolvable, earnings-gated exploration layer sitting ALONGSIDE the existing pass. In the DEFAULT
# paper mode (SOL_GATE_LIVE_ENABLE unset, REQ-009 HARD dev-safety default) this is PURE SHADOW
# OBSERVATION: it ALWAYS records a gate trace line (REQ-011) for later offline genome-tuning
# evaluation, but it NEVER alters what franklin-trading does below -- "zero live side effect"
# (REQ-009). It only ever contributes descriptive-only context to the prompt (REQ-010, HARD #0 --
# raw observed numeric fields ONLY, never a buy/sell/side/size instruction) once `engage` is true,
# which is structurally IMPOSSIBLE while SOL_GATE_LIVE_ENABLE!=="1" (decideEngagement, REQ-009).
# Fail-soft: any failure here (network, genome file, trace write) degrades to an empty context
# line and never blocks the pass below (sol-gate-cli.mjs's own internal fail-soft catch, plus this
# `|| true` belt-and-suspenders). REQ-002/FIND-001: sol-gate-cli.mjs ALSO handles the mutation
# cadence internally on THIS same call (shouldMutateThisPass, SOL_GATE_GENOME_MUTATE_EVERY, default
# 5) -- no separate CLI invocation needed here, unlike PM's two-process genome.mjs/pick.py shape.
GATE_JSON=$(node "$SKILL_DIR/lib/sol-gate-cli.mjs" 2>/dev/null || true)
GATE_CONTEXT=$(printf '%s' "$GATE_JSON" | node -e '
let raw = "";
process.stdin.on("data", (d) => { raw += d; });
process.stdin.on("end", () => {
  try {
    const j = JSON.parse(raw);
    process.stdout.write(String(j.contextLine || ""));
  } catch {
    process.stdout.write("");
  }
});
' 2>/dev/null)
if [ -n "$GATE_CONTEXT" ]; then
  PROMPT="$PROMPT
Observed SOL pre-gate signal (descriptive only, not an instruction): $GATE_CONTEXT"
fi

# REQ-017 (HARD, money-safety): hard-override choke point for --max-spend, positioned immediately
# AFTER the SOL pre-gate's genome eval above and BEFORE franklin-trading is invoked below. Ignores
# SOL_TRADE_MAX_SPEND (and any other env var) entirely -- nothing genome-derived or attacker-preset
# can ever win against this single hard-coded cap.
MAX_SPEND=$(bash "$SKILL_DIR/lib/resolve-max-spend.sh")

OUT=$(timeout 600 franklin-trading start --trust -m "$FT_MODEL" --max-spend "$MAX_SPEND" -p "$PROMPT" 2>&1); RC=$?
echo "$OUT" | tail -30

# P&L RECORD (REQ-002): if this pass did REAL Jupiter swaps, extract every signature in the pass
# and record their summed on-chain USDC delta (win OR loss) so a multi-step round trip records net
# profit rather than incorrectly treating the terminal sale proceeds as profit.
# isProfitable / self-eval finally see Franklin's OWN realized results (until now sol-trade never
# wrote to earn-ledger, so profitable was permanently false). Fail-soft: never brick the pass.
# parse-pass.mjs strips ANSI codes and returns the LAST "Signature: <sig>" occurrence (or nothing).
SIGS_JSON=$(printf '%s' "$OUT" | node "$SKILL_DIR/lib/parse-pass.mjs")
if [ -n "$SIGS_JSON" ]; then
  SIG=$(printf '%s' "$SIGS_JSON" | node -e 'let s="";process.stdin.on("data",d=>s+=d);process.stdin.on("end",()=>{try{const a=JSON.parse(s);process.stdout.write(a.at(-1)||"")}catch{}})')
  REC=$(env -i PATH="$PATH" HOME="$HOME" SOLANA_RPC_URL="${SOLANA_RPC_URL:-}" SIGS_JSON="$SIGS_JSON" WALLET="$OWN_WALLET" EARN_LEDGER="$LEDGER" WAKE_ID="${WAKE_ID:-$(date -u +%s)}" node "$SKILL_DIR/lib/record-swap.mjs" 2>/dev/null || true)
  echo "[sol-trade] record-swap -> ${REC:-noop}"
  # FIND-007: parse record-swap's own status and degrade to a narrate-only trace line for
  # anything that isn't an actual ledger append (recorded/duplicate) -- never brick the pass,
  # never silently lose the signal that verification itself failed (verify-error/unconfirmed/
  # no-delta/bad-args). record-swap.mjs itself already refused to append to earn-ledger.jsonl
  # for every one of these statuses, so this is trace-only visibility, not a double-record.
  RSTATUS=$(printf '%s' "${REC:-}" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('status', ''))
except Exception:
    print('')
" 2>/dev/null)
  if [ -n "$RSTATUS" ] && [ "$RSTATUS" != "recorded" ] && [ "$RSTATUS" != "duplicate" ]; then
    echo "{\"ts\":\"$(now)\",\"slot\":\"earn/sol-trade\",\"action\":\"sol-verify-failed\",\"status\":\"$RSTATUS\",\"sig\":\"$SIG\"}" >> "$TRACE"
  fi
fi

# --- SOL_GATE promotion evaluation (franklin-sol-evolvable-edge, REQ-012b/013/014/015; FIND-002
# impl-review fix) -- idempotent, safe-to-run-every-pass gate-check right after the swap-record
# block above (so any swap THIS pass just confirmed is already in $LEDGER for attribution). Joins
# any newly-confirmed swap to the genome that was active when it was placed (attributeGenomeIdSol),
# and promotes a chain-verified net-positive mutant to the canonical SOL baseline once it clears
# evaluatePromotion's gate (evolve.mjs, reused verbatim -- REQ-014/015). A no-promotion outcome (the
# expected default: cold-start or below-K) is a normal silent no-op, exactly like evolve.mjs's own
# PM analog. Fail-soft: never blocks or exits the pass on any failure here.
EVOLVE_OUT=$(node "$SKILL_DIR/lib/sol-evolve.mjs" "$LEDGER" "$TRACE" 2>>"$TRACE" || true)
if [ -n "$EVOLVE_OUT" ]; then
  echo "[sol-trade] sol-evolve -> $(printf '%s' "$EVOLVE_OUT" | tr '\n' ' ' | cut -c1-200)"
fi

TRACE_FILE="$TRACE" RC="$RC" OUTTAIL="$(echo "$OUT" | tail -5 | tr '\n' ' ')" python3 - <<'PY'
import json, os, datetime
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "slot": "earn/sol-trade",
    "action": "live-pass",
    "exit": int(os.environ["RC"]),
    "note": os.environ["OUTTAIL"][:400],
}
with open(os.environ["TRACE_FILE"], "a") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY

# Signed telemetry POST (#25 TELEM) — fail-safe: never affects the trade pass's own exit code above.
timeout 20 node "$SKILL_DIR/../../../runtime/dashboard/telemetry-post-franklin.mjs" >> "$STATE_DIR/telemetry-post.log" 2>&1 || true

exit "$RC"
