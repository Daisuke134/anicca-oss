#!/usr/bin/env python3
"""Pure-logic tests for daily_loss_guard.py's check_daily_loss (the daily-scoped circuit-breaker
that was missing relative to hummingbot's kill_switch_rate / alpha-mcp's "Daily loss limit: 5%").
NEVER touches the real ledger file or the KILL file — all inputs are mock dicts / injected now_ts.

Run: python3 -m pytest test_daily_loss_guard.py -q     (or: python3 test_daily_loss_guard.py)
"""
import datetime
import os
import tempfile
from unittest import mock

import daily_loss_guard
from daily_loss_guard import check_daily_loss, _day_bounds_utc

# Fixed "now": 2026-07-25 12:00:00 UTC
NOW = datetime.datetime(2026, 7, 25, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
TODAY_09 = datetime.datetime(2026, 7, 25, 9, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
YESTERDAY_23 = datetime.datetime(2026, 7, 24, 23, 0, 0, tzinfo=datetime.timezone.utc).timestamp()


def test_disabled_guard_never_halts():
    rows = [{"ts": TODAY_09, "source": "polymarket-redeem", "net_usdc": -100}]
    out = check_daily_loss(rows, NOW, limit_usd=None)
    assert out["halted"] is False
    assert out["today_net_usd"] == 0.0


def test_no_rows_no_halt():
    out = check_daily_loss([], NOW, limit_usd=3.0)
    assert out["halted"] is False
    assert out["today_net_usd"] == 0.0
    assert out["rows_counted"] == 0


def test_small_loss_under_limit_no_halt():
    rows = [{"ts": TODAY_09, "source": "polymarket-redeem", "net_usdc": -1.5}]
    out = check_daily_loss(rows, NOW, limit_usd=3.0)
    assert out["halted"] is False
    assert out["today_net_usd"] == -1.5


def test_loss_exactly_at_limit_halts():
    rows = [{"ts": TODAY_09, "source": "polymarket-redeem", "net_usdc": -3.0}]
    out = check_daily_loss(rows, NOW, limit_usd=3.0)
    assert out["halted"] is True
    assert "daily-loss-limit-breached" in out["reason"]


def test_loss_over_limit_halts():
    rows = [
        {"ts": TODAY_09, "source": "polymarket-merge", "net_usdc": -2.0},
        {"ts": TODAY_09, "source": "polymarket-trade", "net_usdc": -1.5},
    ]
    out = check_daily_loss(rows, NOW, limit_usd=3.0)
    assert out["halted"] is True
    assert out["today_net_usd"] == -3.5
    assert out["rows_counted"] == 2


def test_yesterdays_loss_does_not_count_today():
    rows = [{"ts": YESTERDAY_23, "source": "polymarket-redeem", "net_usdc": -50}]
    out = check_daily_loss(rows, NOW, limit_usd=3.0)
    assert out["halted"] is False
    assert out["today_net_usd"] == 0.0


def test_other_source_ignored():
    # a big loss from an unrelated strategy (e.g. sol-trade) must never trip polymarket's guard
    rows = [{"ts": TODAY_09, "source": "sol-trade", "net_usdc": -100}]
    out = check_daily_loss(rows, NOW, limit_usd=3.0)
    assert out["halted"] is False
    assert out["today_net_usd"] == 0.0


def test_gains_offset_losses_same_day():
    rows = [
        {"ts": TODAY_09, "source": "polymarket-redeem", "net_usdc": 5.0},
        {"ts": TODAY_09, "source": "polymarket-redeem", "net_usdc": -2.0},
    ]
    out = check_daily_loss(rows, NOW, limit_usd=3.0)
    assert out["halted"] is False
    assert out["today_net_usd"] == 3.0


def test_falls_back_to_earn_minus_cost_when_net_missing():
    rows = [{"ts": TODAY_09, "source": "polymarket-redeem", "earn_usdc": 1.0, "cost_usdc": 5.0}]
    out = check_daily_loss(rows, NOW, limit_usd=3.0)
    assert out["halted"] is True
    assert out["today_net_usd"] == -4.0


def test_malformed_rows_ignored():
    rows = [None, "x", {"ts": TODAY_09}, {"ts": TODAY_09, "source": "polymarket-redeem"}]
    out = check_daily_loss(rows, NOW, limit_usd=3.0)
    assert out["halted"] is False
    assert out["today_net_usd"] == 0.0


def test_day_bounds_are_midnight_utc():
    start, end = _day_bounds_utc(NOW)
    start_dt = datetime.datetime.fromtimestamp(start, tz=datetime.timezone.utc)
    assert start_dt.hour == 0 and start_dt.minute == 0 and start_dt.second == 0
    assert end == NOW


def test_daily_loss_halt_writes_shared_external_kill_path():
    with tempfile.TemporaryDirectory() as directory:
        kill = os.path.join(directory, "polymarket", "KILL")
        with mock.patch.object(daily_loss_guard, "KILL_SWITCH", kill):
            daily_loss_guard.write_kill_switch("daily-loss")
        assert os.path.isfile(kill)
        assert "daily-loss" in open(kill, encoding="utf-8").read()


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
