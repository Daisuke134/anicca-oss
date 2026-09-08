#!/usr/bin/env python3
"""pinnacle_observe.py -- OBSERVATION-ONLY wrapper around pinnacle_edge.py for run.sh.

WHY THIS EXISTS (2026-07-12, see docs/loop-engineering/30-pinnacle-edge-measurement.md):
pinnacle_edge.py is proven-correct (32/32 tests, three real false-edges caught and killed) but the
first live measurement found ZERO comparable games -- Pinnacle carries tomorrow's pre-match lines
while Polymarket lists today's already-started games, so the two data sources never overlapped in
that one 20-minute window. n=0 means the edge's existence is UNKNOWN, not "no" and not "yes". The
correct next step is to accumulate observations across many wake-ups until games that are
pre-match on BOTH sides at once actually appear, and only then judge whether the disagreement is
real. This module is that accumulation step and NOTHING else.

HARD CONSTRAINT: this module NEVER places an order. It does not import place_order.py,
bundle_arb.py, or market_maker.py, and it does not call the CLOB client. It only reads (via
pinnacle_edge.py's own read-only fetchers) and appends one JSON line per invocation to
state/pinnacle-observations.jsonl -- including when zero games were comparable, because that
absence is itself the data point this exists to collect. PINNACLE_LIVE (a future env-gated
trading switch) does not exist anywhere in this repo yet; wiring it up is a separate, later
change, not something this file should anticipate with dead code.

Called by run.sh once per pass, after the existing strategies, with `|| true` around it -- a
failure here must never fail the pass or place a bet. pinnacle_edge.py's own MIN_FETCH_INTERVAL_S
cache already caps the paid Odds-API reads to ~5/day; this file adds no extra throttling.
"""
from __future__ import annotations

import datetime
import json
import os
import sys

import pinnacle_edge as pe

STATE_DIR = os.environ.get(
    "LIFE_MANAGER_STATE_ROOT", os.path.expanduser("~/.local/state/life-manager/polymarket")
)
OBSERVATIONS_PATH = os.path.join(STATE_DIR, "pinnacle-observations.jsonl")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_odds_api_key() -> str | None:
    """Return the installation environment's optional Odds API key."""
    return os.environ.get("ODDS_API_KEY")


def _funnel_matched_comparable(pin_events: list[dict], pm_markets: list[dict], now_ts: float):
    """Instrumentation-only counts alongside find_edges' OWN filtering order (prematch ->
    match_market -> h2h shape -> priceable), so the funnel numbers describe exactly what
    find_edges itself would have compared. This does NOT duplicate find_edges' edge/threshold
    math -- that stays pinnacle_edge.py's single source of truth; find_edges() is called
    separately below for the real edges list.

    matched     = pre-match Pinnacle events paired to the SAME Polymarket game (match_market).
    comparable  = of those, the ones whose Pinnacle h2h is 2-way AND Polymarket has priceable
                  outcomes -- i.e. an actual apples-to-apples probability comparison was possible.
    """
    matched = 0
    comparable = 0
    for ev in pin_events:
        if not pe.is_prematch(ev.get("commence_time", ""), now_ts):
            continue
        pm, _score = pe.match_market(ev, pm_markets)
        if not pm:
            continue
        matched += 1
        books = [b for b in (ev.get("bookmakers") or []) if b.get("key") == "pinnacle"]
        if not books:
            continue
        h2h_markets = [m for m in (books[0].get("markets") or []) if m.get("key") == "h2h"]
        if not h2h_markets:
            continue
        if len(h2h_markets[0].get("outcomes") or []) != 2:
            continue
        if not pe.polymarket_prices(pm):
            continue
        comparable += 1
    return matched, comparable


def _empty_funnel() -> dict:
    return {
        "pinnacle_events": 0, "pinnacle_prematch": 0, "pm_markets": 0,
        "matched": 0, "comparable": 0,
    }


def observe(
    api_key: str,
    now_ts: float | None = None,
    sports: list[str] | None = None,
    min_edge: float | None = None,
    max_edge: float | None = None,
    fetch_pm=None,
    fetch_pin_budgeted=None,
) -> dict:
    """Runs one read-only scan and returns the observation dict. NEVER raises -- any failure
    (network, parsing, an unexpected pinnacle_edge bug) is caught and returned as an
    {"error": ...} record with a zeroed funnel, so the caller can always append exactly one
    well-formed line rather than lose the pass."""
    ts = _now_iso()
    fetch_pm = fetch_pm or pe.fetch_polymarket_sports
    fetch_pin_budgeted = fetch_pin_budgeted or pe.fetch_pinnacle_budgeted
    now_ts = now_ts if now_ts is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    sports = sports if sports is not None else list(pe.DEFAULT_SPORTS)
    min_edge = min_edge if min_edge is not None else float(os.environ.get("PINNACLE_MIN_EDGE", "0.03"))
    max_edge = max_edge if max_edge is not None else float(os.environ.get("PINNACLE_MAX_EDGE", "0.30"))

    try:
        pm = fetch_pm()
        pin, cached = fetch_pin_budgeted(sports, api_key, now_ts)
        prematch = sum(1 for e in pin if pe.is_prematch(e.get("commence_time", ""), now_ts))
        matched, comparable = _funnel_matched_comparable(pin, pm, now_ts)
        edges = pe.find_edges(pin, pm, min_edge=min_edge, max_edge=max_edge, now_ts=now_ts)
        return {
            "ts": ts,
            "funnel": {
                "pinnacle_events": len(pin),
                "pinnacle_prematch": prematch,
                "pm_markets": len(pm),
                "matched": matched,
                "comparable": comparable,
            },
            "edges": edges,
            "odds_from_cache": cached,
        }
    except Exception as e:  # fail-soft (spec §4): a scan failure is data, not a crash
        return {
            "ts": ts,
            "funnel": _empty_funnel(),
            "edges": [],
            "odds_from_cache": None,
            "error": f"{type(e).__name__}: {e}",
        }


def append_observation(obs: dict, path: str | None = None) -> None:
    path = path or OBSERVATIONS_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(obs, ensure_ascii=False) + "\n")
    except Exception:
        pass  # a logging failure must never break the pass


def main() -> int:
    try:
        api_key = resolve_odds_api_key()
        if not api_key:
            return 0  # silent skip -- no key configured, no jsonl line, no error
        obs = observe(api_key)
        append_observation(obs)
    except Exception:
        pass  # last-resort fail-soft: this module must never fail run.sh's pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
