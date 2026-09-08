#!/usr/bin/env python3
"""
pick.py — ALPHA: PM autonomous market/side/size picker (#25 spec §2.1).

NOTHING about WHICH market or WHICH side is hardcoded here (HARD #0 / #1 of
this task). The MODEL decides:
  - multi-model consensus (AIAnalyzer.consensus_analysis, 3 FREE BlockRun
    models — NVIDIA-hosted llama-4-maverick + qwen3-next + mistral-nemotron,
    $0 inference, #31; env-overridable via EARN_CONSENSUS_MODELS) estimates the
    true probability and votes BULLISH/BEARISH/MIXED,
  - smart-money / whale signal (get_smart_money_summary) is fed into that
    same consensus call as context.
This file only: fetches candidates, filters/sorts them (resolve-soon +
liquidity — bookkeeping, not judgment), runs the model's consensus+whale
analysis, GATES on the model's own edge/confidence/consensus output, sizes
via Kelly, and CAPS the size (money-safety, not judgment). Fails CLOSED to
WAIT whenever a signal is missing or nothing clears the gate — it never
picks a default market/side.

Env (all optional, defaults shown — self-improve loop tunes these, spec §4):
  MIN_EDGE=0.15            minimum |avg_edge| from consensus to act
  MIN_CONF=7               minimum avg_confidence (1-10 scale) to act
  RESOLVE_HORIZON_DAYS=14  drop candidates resolving further out than this
  MIN_LIQUIDITY=5000       USD, passed straight to fetch_active_markets
  MIN_ODDS=0.15            YES-odds floor, passed straight to fetch_active_markets
  MAX_ODDS=0.85            YES-odds ceiling, passed straight to fetch_active_markets
  MAX_BET_SIZE=2           USD hard cap on the sized bet (money-safety, #26/#28)
  MAX_CANDIDATES=5         how many resolve-soonest candidates to run
                           consensus+whale analysis on (cost bound, not judgment)

Output: exactly one line of JSON on the REAL stdout (guaranteed clean — see below).
  qualifying candidate found ->
    {"token_id","side":"BUY","outcome":"YES"|"NO","amount","market","end_date",
     "edge","confidence","consensus"}
  nothing qualifies, or a required signal is unavailable ->
    {"action":"WAIT","reason":"<why>"}   (fail-closed — never a default bet)

CLEAN-STDOUT GUARANTEE (#25 adversary fix, accounting integrity, 2026-07-05):
a real Franklin fill was once mis-logged ok:false because an imported module
printed to stdout and merged onto the same line as the result JSON, breaking
run.sh's json.loads(). Fix: `sys.stdout` at process start is captured as
`_REAL_STDOUT` before any heavy import; ALL work (imports, market fetch, AI
analysis, whale signal) runs under `contextlib.redirect_stdout(sys.stderr)`,
so any stray print() from this file's own imports or the base agent's src.*
modules lands on stderr. The ONLY thing ever written to `_REAL_STDOUT` is the
single final `_emit(...)` call below — stdout is guaranteed to be exactly one
clean JSON line.
"""
import os
import sys
import json
import asyncio
import contextlib
from datetime import datetime, timezone

_REAL_STDOUT = sys.stdout  # capture BEFORE any import that might print


def _emit(obj):
    """The ONLY function allowed to write to the real, captured stdout."""
    print(json.dumps(obj), file=_REAL_STDOUT, flush=True)


VENDOR_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor", "polymarket_agent_src")
sys.path.insert(0, VENDOR_ROOT)

with contextlib.redirect_stdout(sys.stderr):
    from market.polymarket import fetch_active_markets  # noqa: E402
    from analysis.ai_analyzer import get_analyzer  # noqa: E402
    from signals.trades import get_smart_money_summary  # noqa: E402
    from utils.kelly import KellyCriterion  # noqa: E402

    # Lives next to this file (the skill dir), not in the vendored agent tree.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from news_search import build_question_with_news, search_news  # noqa: E402


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


MIN_EDGE = _env_float("MIN_EDGE", 0.15)
MIN_CONF = _env_float("MIN_CONF", 7)
RESOLVE_HORIZON_DAYS = _env_float("RESOLVE_HORIZON_DAYS", 14)
MIN_LIQUIDITY = _env_float("MIN_LIQUIDITY", 5000.0)
MIN_ODDS = _env_float("MIN_ODDS", 0.15)
MAX_ODDS = _env_float("MAX_ODDS", 0.85)
MAX_BET_SIZE = _env_float("MAX_BET_SIZE", 2.0)
POLY_MIN_ORDER = _env_float("POLY_MIN_ORDER", 1.0)  # Polymarket min marketable order value ($1) — exchange constraint, not judgment
MAX_CANDIDATES = _env_int("MAX_CANDIDATES", 5)


def wait(reason):
    """Fail-closed exit: emit WAIT, never a default market/side/bet.
    Uses _emit (real stdout) so it's clean even when called from inside the
    redirect_stdout(sys.stderr) block in main()."""
    _emit({"action": "WAIT", "reason": reason})
    sys.exit(0)


def parse_end_date(end_date_str):
    """format_market emits '%Y-%m-%d %H:%M' UTC, or 'Unknown' if unparseable."""
    try:
        return datetime.strptime(end_date_str, "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return None


def short_dated_candidates(markets, horizon_days):
    """Resolve-soon first (primary key), volume desc (secondary). Drops
    anything beyond RESOLVE_HORIZON_DAYS or missing a parseable end_date
    (can't confirm short-dated -> excluded, fail-closed on the horizon) or
    missing both YES/NO token ids (can't route an order)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    horizon_seconds = horizon_days * 86400
    dated = []
    for m in markets:
        end_dt = parse_end_date(m.get("end_date"))
        if end_dt is None:
            continue
        if (end_dt - now).total_seconds() > horizon_seconds:
            continue
        if len(m.get("token_ids") or []) < 2:
            continue
        dated.append((end_dt, m))
    dated.sort(key=lambda pair: (pair[0], -float(pair[1].get("volume", 0) or 0)))
    return [m for _, m in dated]


def whale_signal(market_id):
    """Best-effort smart-money read. Missing/failed signal is NOT fatal —
    consensus_analysis just runs without whale context that round; the model
    still has to clear the same edge/confidence/consensus gate."""
    try:
        return asyncio.run(get_smart_money_summary(market_id))
    except Exception:
        return None


def evaluate(analyzer, market):
    """Run the model's own consensus+whale judgment on one candidate.
    Returns the consensus_analysis result dict, or None if the model
    couldn't produce one (never a fabricated result).

    The model is shown a web search of the market's subject BEFORE it prices it.
    Without that, it sees only the question and the market's own price — and a model
    with no outside information cannot out-predict a price thousands of traders
    already set. A failed search yields no news and the pass proceeds as before.
    """
    market_id = market.get("id") or market.get("condition_id")
    whale_data = whale_signal(market_id) if market_id else None
    news_text, news_urls = search_news(market["question"])
    # Visible on stderr (never stdout — stdout is the one-line JSON contract). Without
    # this line there is no way to tell a searched judgment from a blind one after the
    # fact, and an invisible feature is indistinguishable from a missing one.
    print(
        f"[news] {len(news_urls)} source(s) for: {market['question'][:70]}",
        file=sys.stderr,
        flush=True,
    )
    question = build_question_with_news(market["question"], news_text)
    try:
        result = analyzer.consensus_analysis(
            question, market["yes_odds"], whale_data=whale_data
        )
    except Exception:
        return None
    if not result or result.get("consensus") == "ERROR":
        return None
    # Carry the evidence with the decision: every bet this engine places must be
    # traceable to what it actually read. A bet with no sources is a coin flip.
    result["news_urls"] = news_urls
    result["news_found"] = bool(news_text)
    return result


def size_bet(kelly, ai_prob, market_prob, avg_edge, avg_conf):
    """Kelly-size the bet, capped by MAX_BET_SIZE (money-safety, not
    judgment — market/side is already decided by the model)."""
    kelly_result = kelly.kelly_bet_size(ai_prob, market_prob)
    amount = float(kelly_result.get("bet_size", 0) or 0)
    if amount <= 0:
        # Fallback sizing for a candidate that ALREADY cleared the edge/
        # confidence/consensus gate but whose full Kelly formula degenerated
        # to zero (e.g. rounding at the fractional-Kelly cap). Never used to
        # force a bet on a candidate that didn't qualify.
        amount = kelly.bankroll * abs(avg_edge) * (avg_conf / 10.0) * 0.5
    amount = min(amount, MAX_BET_SIZE)
    # Floor at the exchange minimum ($1): a marketable BUY below POLY_MIN_ORDER is
    # rejected by Polymarket ("invalid amount ... min size: 1"). Platform constraint,
    # not judgment. If the cap is below the min, no valid bet exists -> 0 -> caller WAITs.
    if amount < POLY_MIN_ORDER:
        amount = POLY_MIN_ORDER if MAX_BET_SIZE >= POLY_MIN_ORDER else 0.0
    return round(amount, 2)


def _run():
    """All the actual work. Called ONLY from inside main()'s
    redirect_stdout(sys.stderr) block, so any print() anywhere in this call
    graph (this file's own helpers, or the base agent's src.* modules —
    fetch_active_markets, consensus_analysis, get_smart_money_summary, etc.)
    lands on stderr, never merging onto the final result line."""
    analyzer = get_analyzer()
    if analyzer is None:
        wait("analyzer-unavailable")

    try:
        markets = fetch_active_markets(
            limit=100,
            min_odds=MIN_ODDS,
            max_odds=MAX_ODDS,
            min_liquidity=MIN_LIQUIDITY,
        )
    except Exception as e:
        wait(f"fetch-markets-error:{e}")

    if not markets:
        wait("no-markets")

    candidates = short_dated_candidates(markets, RESOLVE_HORIZON_DAYS)
    if not candidates:
        wait("no-short-dated-liquid-candidates")

    kelly = KellyCriterion(min_edge_pct=MIN_EDGE)
    best = None  # (score, market, consensus_result)

    for market in candidates[:MAX_CANDIDATES]:
        result = evaluate(analyzer, market)
        if result is None:
            continue

        avg_edge = float(result.get("avg_edge", 0.0))
        avg_conf = float(result.get("avg_confidence", 0.0))
        consensus = result.get("consensus")

        if consensus == "MIXED":
            continue
        if abs(avg_edge) < MIN_EDGE or avg_conf < MIN_CONF:
            continue

        score = abs(avg_edge) * avg_conf
        if best is None or score > best[0]:
            best = (score, market, result)

    if best is None:
        wait("no-candidate-cleared-edge-confidence-gate")

    _, market, result = best
    avg_edge = float(result["avg_edge"])
    avg_conf = float(result["avg_confidence"])
    consensus = result["consensus"]
    market_prob = float(result.get("market_probability", market["yes_odds"]))
    ai_prob = float(result.get("avg_probability", market_prob))

    outcome = "YES" if consensus == "BULLISH" else "NO"
    token_ids = market.get("token_ids") or []
    token_id = token_ids[0] if outcome == "YES" else token_ids[1]
    if not token_id:
        wait("missing-token-id-for-outcome")

    amount = size_bet(kelly, ai_prob, market_prob, avg_edge, avg_conf)
    if amount <= 0:
        wait("zero-size-after-cap")

    _emit({
        "token_id": token_id,
        "side": "BUY",
        "outcome": outcome,
        "amount": amount,
        "market": market["question"],
        "end_date": market["end_date"],
        "edge": avg_edge,
        "confidence": avg_conf,
        "consensus": consensus,
        # The evidence this bet rests on. An empty list means the model priced the
        # market on the price alone — i.e. a coin flip, and readable as such later.
        "news_urls": result.get("news_urls", []),
        "news_found": bool(result.get("news_found")),
    })


def main():
    """Entry point. Redirects sys.stdout to stderr for the ENTIRE run so
    stdout stays clean; _emit()/wait() write to _REAL_STDOUT directly, so
    they're unaffected by the redirect."""
    with contextlib.redirect_stdout(sys.stderr):
        _run()


if __name__ == "__main__":
    main()
