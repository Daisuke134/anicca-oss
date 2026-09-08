#!/usr/bin/env python3
"""Pure-logic tests for decision_loop.py's classify() (deterministic bookkeeping over
ALREADY-PRINTED strategy output — never invents a decision/reason that wasn't actually printed).

Run: python3 -m pytest test_decision_loop.py -q     (or: python3 test_decision_loop.py)
"""
import os
import tempfile

import decision_loop
from decision_loop import classify


def test_bundle_arb_no_edge():
    out = classify(
        "deposit wallet: 0xabc | pUSD: 5.18\nscanned 60 markets.\n"
        "no risk-free bundle arb ≥0.5% right now (market efficient). MM keeps quoting.\n",
        0,
    )
    assert out["decision"] == "no_trade"
    assert "no risk-free bundle arb" in out["reason"]
    assert out["scanned_markets"] == 60
    assert out["naked_leg_warning"] is False


def test_bundle_arb_hold_budget():
    out = classify(
        "scanned 60 markets.\nHOLD: MAX_PASS_SPEND $2.00 can't afford 5 shares of both legs "
        "(needs $4.95). No order placed this pass.\n",
        0,
    )
    assert out["decision"] == "no_trade"
    assert "HOLD" in out["reason"]


def test_bundle_arb_would_trade_dry():
    out = classify(
        "scanned 60 markets.\nARB FOUND: some market | ask_YES 0.4+ask_NO 0.55=0.950 | locked edge 5.00%\n"
        "buying 5 YES + 5 NO (FOK) = locked $0.250 profit...\n"
        "  [DRY] would create_market_order BUY 5@0.4 token=111 (FOK) — no order placed\n"
        "  [DRY] would create_market_order BUY 5@0.55 token=222 (FOK) — no order placed\n",
        0,
    )
    assert out["decision"] == "would_trade"
    assert "ARB FOUND" in out["reason"]


def test_market_maker_no_bundle_found():
    out = classify("deposit wallet pUSD: 5.18\nno profitable maker-bundle market found this pass.\n", 0)
    assert out["decision"] == "no_trade"
    assert "no profitable maker-bundle" in out["reason"]


def test_market_maker_naked_leg_takes_priority():
    stdout = (
        "deposit wallet pUSD: 5.18\n"
        "  [DRY] would NAKED-FIX complete 111 8@0.255 mkt=Will there be no change in Fed\n"
        "  naked leg handled this pass; skipping new maker quotes (fail-closed).\n"
    )
    out = classify(stdout, 0)
    assert out["decision"] == "no_trade"
    assert out["naked_leg_warning"] is True
    assert "naked" in out["reason"].lower()


def test_market_maker_would_quote_dry():
    stdout = (
        "MKT Some Market                              YES 0.4+NO 0.55=0.950 lock 5.0% -> 5 sh/leg\n"
        "    [DRY] would post_order YES 5@0.4 (post_only BUY) — no order placed\n"
        "    [DRY] would post_order NO  5@0.55 (post_only BUY) — no order placed\n"
    )
    out = classify(stdout, 0)
    assert out["decision"] == "would_trade"


def test_error_on_nonzero_exit_with_no_markers():
    out = classify("Traceback (most recent call last):\nValueError: boom\n", 1)
    assert out["decision"] == "error"
    assert "exit=1" in out["reason"]


def test_empty_output_exit_zero_is_no_trade_unknown():
    out = classify("", 0)
    assert out["decision"] == "no_trade"
    assert "no decision line matched" in out["reason"]


def test_shared_external_kill_stops_decision_cycle_before_strategies():
    with tempfile.TemporaryDirectory() as directory:
        kill = os.path.join(directory, "polymarket", "KILL")
        os.makedirs(os.path.dirname(kill))
        with open(kill, "w", encoding="utf-8") as handle:
            handle.write("daily-loss\n")
        previous = decision_loop.KILL_FILE
        decision_loop.KILL_FILE = kill
        try:
            result = decision_loop.run_cycle()
        finally:
            decision_loop.KILL_FILE = previous
        assert result["action"] == "skip"
        assert result["strategies_run"] == []
        assert "daily-loss" in result["reason"]


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
