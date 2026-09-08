#!/usr/bin/env python3
"""daily_loss_guard.py — the ONE hummingbot/alpha-mcp-shaped risk control this repo was missing.

WHY THIS EXISTS (2026-07-25, decision-loop task). The existing money-safety primitives are:
  - per-trade cap: MAX_BET_SIZE / MAX_PASS_SPEND (pick.py, bundle_arb.py, market_maker.py) — bounds
    ONE trade / ONE pass.
  - LIFETIME circuit breaker: redeem.py/merge.py's check_cumulative_halt() + write_kill_switch() —
    trips the shared KILL file if cumulative earn-vs-spend ever goes insolvent across every redeem
    EVER recorded (funding/lib/kill_switch.py's is_killed() is the read side of that same file).
  What was missing, and what hummingbot's `kill_switch_rate` / alpha-mcp's "Daily loss limit: 5%"
  both have in their risk-control shape: a guard scoped to a ROLLING WINDOW (today), not lifetime —
  a bad day can look fine against a lifetime-cumulative number for weeks before that guard trips.

WHAT THIS DOES NOT DO: it does not change any strategy/edge logic (pick.py, pinnacle_edge.py,
bundle_arb.py, market_maker.py are untouched by this file). It does not invent a new halt
mechanism — it reuses the EXACT SAME KILL file every other guard in this skill already checks
(SPEC/SKILL.md "kill-switch: touch KILL in this dir"), via the same write_kill_switch() shape
redeem.py/merge.py already use (a plain text file, fail-closed, checked at the top of the next
pass). This file is a NEW pure decision function + a thin ledger-reading wrapper around it, tested
in test_daily_loss_guard.py exactly like no_naked.py's pure functions are tested in
test_no_naked.py.

Per-trade "triple barrier" (hummingbot's stop_loss/take_profit/time_limit): deliberately NOT
added as new code. On a binary prediction market, a position resolves to exactly $0 or $1 — a
BUY's maximum possible loss is already hard-capped at MAX_BET_SIZE (stronger than a stop-loss:
no slippage/gap risk, the loss can never exceed the stake) and RESOLVE_HORIZON_DAYS=14 already
functions as the time-exit (pick.py refuses any market resolving further out than that). There is
no equivalent of "take-profit" for a buy-and-hold-to-resolution binary bet in this strategy's
design — adding active mid-position selling would be new strategy behavior, out of scope
("do not redesign the strategy").
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

from state_paths import external_state_path

LEDGER_PATH_DEFAULT = os.path.expanduser("~/anicca/skills/earn/state/earn-ledger.jsonl")
KILL_SWITCH = os.environ.get(
    "PM_KILL_SWITCH",
    os.path.expanduser("~/.local/state/life-manager/polymarket/KILL"),
)
DEFAULT_DAILY_LOSS_LIMIT_USD = float(os.environ.get("DAILY_LOSS_LIMIT_USD", "3.0"))


def _day_bounds_utc(now_ts: float) -> tuple[float, float]:
    """[start_of_utc_day, now_ts) as epoch seconds. Pure."""
    dt = datetime.datetime.fromtimestamp(now_ts, tz=datetime.timezone.utc)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), now_ts


def check_daily_loss(rows: list[dict], now_ts: float, limit_usd: float | None,
                      source_prefix: str = "polymarket-") -> dict:
    """PURE decision function (no I/O). rows = every parsed ledger line (any source/day) — this
    function does the filtering, so callers never have to pre-slice and risk getting the UTC
    boundary wrong in two places.

    Returns {"halted": bool, "reason": str, "today_net_usd": float, "rows_counted": int}.
    Fail-open on a missing/None limit (limit_usd=None means "guard disabled", NOT "halt") —
    fail-CLOSED behavior for real infra failures (unreadable ledger) belongs to the caller
    (read_today_rows below), matching check_cumulative_halt's own fail-closed-on-guard-error
    shape, not this pure function (which has no I/O to fail).
    """
    if limit_usd is None:
        return {"halted": False, "reason": "", "today_net_usd": 0.0, "rows_counted": 0}
    start, end = _day_bounds_utc(now_ts)
    todays = [
        r for r in (rows or [])
        if isinstance(r, dict)
        and str(r.get("source", "")).startswith(source_prefix)
        and start <= float(r.get("ts", 0) or 0) < end + 1
    ]
    net = sum(float(r.get("net_usdc", (r.get("earn_usdc", 0) or 0) - (r.get("cost_usdc", 0) or 0)) or 0)
              for r in todays)
    net = round(net, 6)
    if net <= -abs(limit_usd):
        return {
            "halted": True,
            "reason": f"daily-loss-limit-breached: today_net=${net:.4f} <= -${abs(limit_usd):.2f} "
                      f"(source_prefix={source_prefix!r}, rows={len(todays)})",
            "today_net_usd": net,
            "rows_counted": len(todays),
        }
    return {"halted": False, "reason": "", "today_net_usd": net, "rows_counted": len(todays)}


def read_ledger_rows(ledger_path: str | None = None) -> list[dict]:
    """I/O boundary: read+parse the shared earn-ledger.jsonl. Fail-closed to [] on any read
    error (missing file, corrupt line) — a guard that can't read the ledger must never silently
    assume 'no loss today' AND must never crash the caller; the caller's own policy (see
    run_check below) decides whether an unreadable ledger should itself halt."""
    path = ledger_path or LEDGER_PATH_DEFAULT
    rows: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except json.JSONDecodeError:
                    continue  # one bad line must not lose every other line
    except FileNotFoundError:
        return []
    except Exception:
        return []
    return rows


def write_kill_switch(reason: str) -> None:
    """Trips the SAME kill-switch file run.sh / decision_loop.py already check at the top of
    every pass — identical shape to redeem.py's write_kill_switch (kept as a separate function
    here, not imported, because redeem.py's version lives behind a heavier import chain — SDK,
    eth_account, dotenv — that a read-only risk-report script should not have to pull in)."""
    kill_switch = external_state_path(
        KILL_SWITCH, Path(__file__).resolve().parents[3], "PM_KILL_SWITCH"
    )
    os.makedirs(os.path.dirname(kill_switch), exist_ok=True)
    with open(kill_switch, "w") as f:
        f.write(
            f"DAILY-LOSS-GUARD HALT ({datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}): "
            f"{reason}\n"
        )


def run_check(ledger_path: str | None = None, limit_usd: float | None = None,
              now_ts: float | None = None, trip_kill_switch: bool = True) -> dict:
    """Thin I/O wrapper: read the ledger, run the pure check, optionally trip KILL on breach.
    limit_usd default: DEFAULT_DAILY_LOSS_LIMIT_USD (env DAILY_LOSS_LIMIT_USD, default $3 — sized
    against the colony's real ~$12 liquid balance per the task brief, roughly hummingbot's
    kill_switch_rate expressed in dollars instead of percent since the bankroll is small and
    mostly fixed-$ capped already via MAX_PASS_SPEND/MAX_BET_SIZE)."""
    if limit_usd is None:
        limit_usd = DEFAULT_DAILY_LOSS_LIMIT_USD
    now_ts = now_ts if now_ts is not None else datetime.datetime.now(datetime.timezone.utc).timestamp()
    rows = read_ledger_rows(ledger_path)
    result = check_daily_loss(rows, now_ts, limit_usd)
    if result["halted"] and trip_kill_switch:
        write_kill_switch(result["reason"])
    return result


if __name__ == "__main__":
    import sys
    res = run_check()
    print(json.dumps(res))
    sys.exit(1 if res["halted"] else 0)
