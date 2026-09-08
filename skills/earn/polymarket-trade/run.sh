#!/bin/bash
# pm-trade run.sh — THIN HARNESS. THIS FILE DECIDES NOTHING. (spec §0.25, #25)
# pick.py (the model: multi-model consensus + smart-money/whale signal) decides
# WHICH market/side/amount; place_order.py executes the working V2 FAK flow with
# THIS instance's own wallet. This wrapper only: kill-switch gate → identity/
# registration → one real live pass (NO dry-run, HARD 0.24) → structured trace (H1).
set -u
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../../.." && pwd -P)"
# shellcheck disable=SC1091
source "$REPO_ROOT/runtime/earn/polymarket-runtime-env.sh" || exit $?
STATE_DIR="$LIFE_MANAGER_STATE_ROOT"
TRACE="$STATE_DIR/pm-trade.trace.jsonl"
KILL_SWITCH="$PM_KILL_SWITCH"
BOUNDED_EXEC="$REPO_ROOT/runtime/loop/bounded-exec.py"

# BRAIN ENV for pick.py's blockrun_llm consensus analyzer (FIX 2026-07-12: pick.py always
# returned WAIT "analyzer-unavailable" because these were unset + the SDK was missing → the
# directional +EV engine never ran. The SDK is now pinned in the managed runtime; point it at the shared
# ClawRouter :8402 (free models = $0) so multi-model consensus actually runs each wake.
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://127.0.0.1:8402/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-x402-local}"
export BLOCKRUN_API_URL="${BLOCKRUN_API_URL:-http://127.0.0.1:8402/v1}"
export BLOCKRUN_BASE_URL="${BLOCKRUN_BASE_URL:-http://127.0.0.1:8402/v1}"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# money-safety guard #1: shared mutable kill-switch written by the redeem guard.
if [ -f "$KILL_SWITCH" ]; then
  echo "{\"ts\":\"$(now)\",\"slot\":\"earn/pm-trade\",\"action\":\"skip\",\"reason\":\"kill-switch\"}" >> "$TRACE"
  exit 0
fi

# --- EVOLVE genome wiring (#19, 2026-07-05) --------------------------------------------------
# load_genome (+ mutate on the exploration cadence) supplies pick.py's EXPLORATION knobs ONLY —
# MIN_EDGE / MIN_CONF / RESOLVE_HORIZON_DAYS / MAX_CANDIDATES / EARN_CONSENSUS_MODELS (spec
# 2026-07-05-evolve-earnings-gated-self-improve-design.md §2.2). Which market/side to bet is
# STILL pick.py's own model judgment (HARD #0, unchanged) — genome never touches that.
# EARN_GENOME_MUTATE_EVERY (default 5) = explore once every M passes; each exploration draw is
# independent from the current baseline+override (never a compounding mutation-of-a-mutation).
GENOME_LIB="$SKILL_DIR/../lib/genome.mjs"
GENOME_COUNTER="$STATE_DIR/genome-pass-counter.json"
if [ -f "$GENOME_LIB" ]; then
  GENOME_ENV="$(EARN_GENOME_COUNTER_FILE="$GENOME_COUNTER" "$LIFE_MANAGER_NODE" "$GENOME_LIB" --maybe-mutate 2>>"$TRACE")"
  if [ -n "$GENOME_ENV" ]; then eval "$GENOME_ENV"; fi
fi
# money-safety (HARD, spec §3): caps are NEVER part of the genome — genome.mjs's own
# FORBIDDEN_CAP_KEYS guard already strips them from every genome object it can ever produce, but
# these lines are run.sh's OWN explicit, auditable "genome (or any env) cannot win" guarantee:
# all three names are fixed here, unconditionally (hard override, not a `:-` soft default), AFTER
# the genome eval, so nothing above — genome or a pre-set env var — can ever override them.
# MAX_PASS_SPEND lives here too (not further down at the strategy-chain site) for symmetry with
# MAX_BET_SIZE/POLY_MIN_ORDER: all THREE caps are asserted fixed at the same single choke point.
export MAX_BET_SIZE=2
export POLY_MIN_ORDER=1
# BUG FIX (2026-07-12, own-eyes+math verified): MAX_PASS_SPEND=2 made bundle_arb.py/
# market_maker.py structurally unable to ever afford Polymarket's 5-share CLOB minimum on an
# ordinary (near-$1 combined YES+NO) market -- budget_shares=int(2/0.97)=2 < 5 -> permanent HOLD,
# no matter how much money is in the wallet (confirmed: still blocks at $5, $40, or more). Raising
# to 20 does not remove the real safety bound: both scripts already take
# min(avail*0.9-or-0.98, MAX_PASS_SPEND), so a pass can still never spend more than ~90-98% of the
# ACTUAL wallet balance -- this constant only stops being a tighter, stricter-than-the-balance
# artificial ceiling on top of that.
export MAX_PASS_SPEND=20
if [ -n "${EARN_GENOME_ID:-}" ]; then
  MIN_EDGE="${MIN_EDGE:-}" MIN_CONF="${MIN_CONF:-}" RESOLVE_HORIZON_DAYS="${RESOLVE_HORIZON_DAYS:-}" \
  MAX_CANDIDATES="${MAX_CANDIDATES:-}" EARN_CONSENSUS_MODELS="${EARN_CONSENSUS_MODELS:-}" \
  EARN_GENOME_ID="$EARN_GENOME_ID" EARN_GENOME_MUTATED="${EARN_GENOME_MUTATED:-0}" TRACE_FILE="$TRACE" \
  "$LIFE_MANAGER_PYTHON" <<'PY'
import json, os, datetime

def num(v):
    try:
        return float(v) if "." in v else int(v)
    except (ValueError, TypeError):
        return v

rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "slot": "earn/pm-trade",
    "action": "genome",
    "genome_id": os.environ["EARN_GENOME_ID"],
    "mutated": os.environ.get("EARN_GENOME_MUTATED") == "1",
    "genome": {
        "MIN_EDGE": num(os.environ.get("MIN_EDGE", "")),
        "MIN_CONF": num(os.environ.get("MIN_CONF", "")),
        "RESOLVE_HORIZON_DAYS": num(os.environ.get("RESOLVE_HORIZON_DAYS", "")),
        "MAX_CANDIDATES": num(os.environ.get("MAX_CANDIDATES", "")),
        "EARN_CONSENSUS_MODELS": os.environ.get("EARN_CONSENSUS_MODELS", ""),
    },
}
with open(os.environ["TRACE_FILE"], "a") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
fi

# #27: ensure THIS instance's deposit wallet is REGISTERED via the bridge Collateral Onramp (idempotent)
# + approve the neg-risk spenders, so EVERY self-funded AI can trade on Polymarket from birth. Registry
# gate is the CONFIRMED root cause of "error resolving address" — never raw-deploy + raw-transfer pUSD;
# always fund THROUGH the bridge (see SKILL.md "DEPOSIT-WALLET REGISTRY GATE" + fund_via_bridge.py).
# Best-effort / non-blocking: an already-registered wallet just re-approves; an unfunded one no-ops.
if [ -f "$SKILL_DIR/fund_via_bridge.py" ]; then
  "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/fund_via_bridge.py" >> "$TRACE" 2>&1 \
    || echo "{\"ts\":\"$(now)\",\"slot\":\"earn/pm-trade\",\"action\":\"register-skip\"}" >> "$TRACE"
fi

# money-safety guard #3 (#25 adversary MUST-FIX): fixed USD ceiling PER STRATEGY, so worst-case
# pass spend is a fixed small number regardless of wallet balance-at-read-time, not a fraction of
# it. bundle_arb.py / market_maker.py / place_order.py each read this independently and cap their
# own leg to it — see SKILL.md "per-pass risk envelope" for the resulting worst-case pass total.
# (MAX_PASS_SPEND is now hard-set once, above, right after the genome eval alongside MAX_BET_SIZE/
# POLY_MIN_ORDER — #19 EVOLVE symmetry fix — so it's already exported by the time we reach here.)

# append_strategy_trace <action> <output-text> <exit-code> — one structured
# trace line per strategy pass (H1). Shared by the two EXISTING working
# strategies below (bundle_arb.py / market_maker.py print human-readable
# text, not JSON, so we tail their output rather than re-parse it).
append_strategy_trace() {
  ACTION="$1" OUT_TEXT="$2" RC="$3" TRACE_FILE="$TRACE" "$LIFE_MANAGER_PYTHON" <<'PY'
import json, os, datetime
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "slot": "earn/pm-trade",
    "action": os.environ["ACTION"],
    "exit": int(os.environ["RC"]),
    "output_tail": os.environ["OUT_TEXT"].strip().splitlines()[-6:],
}
with open(os.environ["TRACE_FILE"], "a") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
}

# STEP 0 — COLLECT WHAT WE ALREADY WON, before risking anything new.
#
# Until 2026-07-12 redeem.py was reachable ONLY from run_earner.sh, i.e. only from the
# claude-p-specific `pm-earner` launchd job. Every other Anicca — Franklin, Franklin2,
# automaton, and anyone who spins up their own — ran THIS file, placed bets, won them,
# and had no way to ever cash them out. Their winnings stayed locked in resolved
# positions forever. That is not an economy; that is a one-way valve.
#
# Now every instance redeems its OWN wallet at the top of its OWN pass (redeem.py derives
# the wallet from this instance's key, so it can only ever collect what it actually won).
# A failed redeem never blocks the pass: an unreachable RPC must not stop the loop from
# trading, and the next wake retries. Winnings free up cash -> cash funds the next bet.
if [ -f "$SKILL_DIR/redeem.py" ]; then
  REDEEM_OUT=$("$LIFE_MANAGER_PYTHON" "$BOUNDED_EXEC" 200 "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/redeem.py" 2>&1); REDEEM_RC=$?
  echo "$REDEEM_OUT" | tail -6
  append_strategy_trace "redeem" "$REDEEM_OUT" "$REDEEM_RC"
fi

# STEP 0b — RECOVER BALANCED OPEN POSITIONS before any cash-gated strategy.
# merge.py burns equal YES/NO shares from one condition back into pUSD. It owns
# selection, approval, receipt verification, and ledger recording; this harness
# only makes the existing recovery primitive reachable from every live pass.
# Fail-soft like redeem: a temporary RPC/relayer failure is traced and retried
# next wake without suppressing the independent earning strategies below.
if [ -f "$SKILL_DIR/merge.py" ]; then
  MERGE_OUT=$("$LIFE_MANAGER_PYTHON" "$BOUNDED_EXEC" 200 "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/merge.py" 2>&1); MERGE_RC=$?
  echo "$MERGE_OUT" | tail -10
  append_strategy_trace "merge" "$MERGE_OUT" "$MERGE_RC"
fi

# BASE STRATEGY #2 — risk-free bundle arbitrage scan (bundle_arb.py, EXISTING
# working alpha, unchanged — do not reinvent). Self-gating: no-ops ("no
# risk-free bundle arb ≥0.5% right now") when the market is efficient; buys
# both legs (FOK) only on a real locked edge.
ARB_OUT=$("$LIFE_MANAGER_PYTHON" "$BOUNDED_EXEC" 200 "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/bundle_arb.py" 2>&1); ARB_RC=$?
echo "$ARB_OUT" | tail -10
append_strategy_trace "bundle-arb" "$ARB_OUT" "$ARB_RC"

# BASE STRATEGY #1 — maker-bundle quoting (market_maker.py, EXISTING working
# alpha, unchanged). Self-gating: HOLDs ("cash < one min bundle") instead of
# spamming failed orders when underfunded; else cancel-and-replace resting
# post-only quotes.
MM_OUT=$("$LIFE_MANAGER_PYTHON" "$BOUNDED_EXEC" 200 "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/market_maker.py" 2>&1); MM_RC=$?
echo "$MM_OUT" | tail -10
append_strategy_trace "market-maker" "$MM_OUT" "$MM_RC"

# PINNACLE OBSERVATION (2026-07-12, OBSERVE-ONLY -- see
# docs/loop-engineering/30-pinnacle-edge-measurement.md). pinnacle_edge.py is proven-correct
# (32/32 tests, three real false-edges caught and killed) but the first live measurement found
# ZERO comparable games -- Pinnacle carries tomorrow's pre-match lines while Polymarket lists
# today's already-started games, so whether the disagreement it looks for is a real, tradeable
# edge is still UNKNOWN (n=0). This block ONLY appends what the scan saw to
# $STATE_DIR/pinnacle-observations.jsonl (via pinnacle_observe.py) so the comparable-game count
# can accumulate across wake-ups; it NEVER places an order -- no place_order.py / bundle_arb.py /
# market_maker.py call happens here, and PINNACLE_LIVE does not exist as a switch anywhere in
# this repo yet (wiring it up to actually trade is a separate, later change, gated on having
# real edge data to judge first). pinnacle_edge.py's own MIN_FETCH_INTERVAL_S cache already caps
# the paid Odds-API reads to ~5/day; this line adds no extra interval control on top.
# pinnacle_observe.py itself is fail-soft (missing ODDS_API_KEY -> silent skip, any fetch
# exception -> a logged {"error":...} line, never a crash) -- the `|| true` here is a second,
# independent layer so a pass can never be failed by this block.
if [ -f "$SKILL_DIR/pinnacle_observe.py" ]; then
  "$LIFE_MANAGER_PYTHON" "$BOUNDED_EXEC" 60 "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/pinnacle_observe.py" >> "$TRACE" 2>&1 || true
fi

# DIRECTIONAL AUTONOMOUS BUY (#25, NEW — the genuinely-missing piece): pick.py
# (multi-model consensus + smart-money/whale signal picks market/side/size,
# fail-closed to WAIT) -> place_order.py (generalized working V2 FAK exec).
# money-safety guard #2: per-trade cap enforced in pick.py (Kelly size, capped)
# AND again in place_order.py (AMOUNT<=MAX_BET_SIZE, default $2). Neither the
# market nor the side is ever decided here — only the model (pick.py) decides.
PICK_OUT=$("$LIFE_MANAGER_PYTHON" "$BOUNDED_EXEC" 300 "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/pick.py" 2>>"$TRACE"); PICK_RC=$?

if [ "$PICK_RC" -ne 0 ] || [ -z "$PICK_OUT" ]; then
  echo "{\"ts\":\"$(now)\",\"slot\":\"earn/pm-trade\",\"action\":\"error\",\"error\":\"pick.py failed rc=$PICK_RC\"}" >> "$TRACE"
  exit 0
fi

# Parse pick.py's one-line JSON. WAIT -> trace + exit 0 (no-churn, no trade).
# Trade -> print a single TAB-separated line the shell can `read` (no bash
# JSON dependency); this is bookkeeping/parsing of a fixed format, not judgment.
# DEFENSE (#25 adversary fix, accounting integrity): pick.py's OWN root fix
# guarantees clean stdout now, but run.sh still recovers defensively — a
# polluted pick must never be misread as a false WAIT (that would silently
# skip a real qualifying candidate).
DECISION=$(PICK_JSON="$PICK_OUT" TRACE_FILE="$TRACE" "$LIFE_MANAGER_PYTHON" <<'PY'
import json, os, datetime

def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def clean(v):
    return str(v).replace("\t", " ").replace("\n", " ")

def recover(raw):
    """Recover a result dict from possibly-noisy stdout: whole-string parse,
    then leading-JSON-object (handles '[JSON][trailing noise]' on one line),
    then last line that parses as a dict. Returns None if truly unrecoverable."""
    raw = (raw or "").strip()
    try:
        o = json.loads(raw)
        if isinstance(o, dict):
            return o
    except Exception:
        pass
    try:
        o, _ = json.JSONDecoder().raw_decode(raw)
        if isinstance(o, dict):
            return o
    except Exception:
        pass
    best = None
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            try:
                o, _ = json.JSONDecoder().raw_decode(ln)
            except Exception:
                continue
        if isinstance(o, dict):
            best = o
    return best

pick = recover(os.environ["PICK_JSON"])
if pick is None:
    pick = {"action": "WAIT", "reason": f"unparseable-pick-output:{os.environ['PICK_JSON'][-160:]!r}"}

if pick.get("action") == "WAIT":
    rec = {"ts": now(), "slot": "earn/pm-trade", "action": "wait", "reason": pick.get("reason")}
    with open(os.environ["TRACE_FILE"], "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("WAIT")
else:
    fields = [clean(pick.get(k, "")) for k in
              ("token_id", "side", "amount", "market", "end_date", "edge", "confidence", "consensus")]
    print("TRADE\t" + "\t".join(fields))
PY
)

if [ "$DECISION" = "WAIT" ]; then
  exit 0
fi

IFS=$'\t' read -r _ TOKEN_ID SIDE AMOUNT MARKET END_DATE EDGE CONFIDENCE CONSENSUS <<< "$DECISION"
export TOKEN_ID SIDE AMOUNT

ORDER_OUT=$("$LIFE_MANAGER_PYTHON" "$BOUNDED_EXEC" 120 "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/place_order.py" 2>>"$TRACE"); ORDER_RC=$?

# DEFENSE (#25 adversary fix, accounting integrity): the SAME recover() as
# above — this is the exact bug that mis-logged Franklin's real filled order
# ("Will Jesus Christ return before GTA VI?", NO, ~$1) as ok:false: place_order's
# stdout had "[valid result JSON][trailing noise]" concatenated on one line, so
# plain json.loads() choked and a real fill got recorded as a failure. place_order.py's
# own root fix guarantees clean stdout now; this stays as a second, independent layer.
ORDER_JSON="${ORDER_OUT:-{}}" MARKET="$MARKET" END_DATE="$END_DATE" EDGE="$EDGE" CONFIDENCE="$CONFIDENCE" CONSENSUS="$CONSENSUS" RC="$ORDER_RC" TRACE_FILE="$TRACE" EARN_GENOME_ID="${EARN_GENOME_ID:-}" "$LIFE_MANAGER_PYTHON" <<'PY'
import json, os, datetime

def recover(raw):
    """Recover a result dict from possibly-noisy stdout: whole-string parse,
    then leading-JSON-object (handles '[JSON][trailing noise]' on one line),
    then last line that parses as a dict. Returns None if truly unrecoverable."""
    raw = (raw or "").strip()
    try:
        o = json.loads(raw)
        if isinstance(o, dict):
            return o
    except Exception:
        pass
    try:
        o, _ = json.JSONDecoder().raw_decode(raw)
        if isinstance(o, dict):
            return o
    except Exception:
        pass
    best = None
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            try:
                o, _ = json.JSONDecoder().raw_decode(ln)
            except Exception:
                continue
        if isinstance(o, dict):
            best = o
    return best

order = recover(os.environ["ORDER_JSON"])
if order is None:
    order = {"ok": False, "error": "unparseable", "raw_tail": os.environ["ORDER_JSON"][-160:]}
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "slot": "earn/pm-trade",
    "action": "trade",
    "market": os.environ.get("MARKET"),
    "end_date": os.environ.get("END_DATE"),
    "edge": os.environ.get("EDGE"),
    "confidence": os.environ.get("CONFIDENCE"),
    "consensus": os.environ.get("CONSENSUS"),
    # EVOLVE (#19): which genome was active for THIS bet, so evolve.mjs can join a later
    # on-chain redeem back to the genome that earned it (attribution, spec §2.3).
    "genome_id": os.environ.get("EARN_GENOME_ID") or None,
    "exit": int(os.environ["RC"]),
    "order": order,
}
with open(os.environ["TRACE_FILE"], "a") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY

exit "$ORDER_RC"
