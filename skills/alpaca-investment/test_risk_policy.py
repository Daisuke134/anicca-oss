from datetime import datetime, timezone
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from risk_policy import evaluate_entry
import allocator
import alpaca_cli
import run as investment_run


NOW = datetime(2026, 9, 6, 14, 0, tzinfo=timezone.utc)


def risk(**changes):
    value = {"allocated_capital_usd": "89.99", "cash_flow_ny_day_usd": "0",
             "equity_pnl_ny_day_usd": "-19.99",
             "official_pnl_ny_day_usd": "-19.99", "risk_day_ready": True,
             "unrealized_pnl_usd": "-10.99", "observed_at": "2026-09-06T13:59:50Z",
             "ny_day": "2026-09-06"}
    value.update(changes)
    return value


class FixedRiskPolicyTest(unittest.TestCase):
    def test_allocator_prompt_exposes_usdc_backed_available_cash(self):
        snapshot = {
            "account": {"cash": "0", "equity": "66.72"},
            "available_cash_usd": "66.72",
            "clock": {"timestamp": "2026-09-10T10:00:00Z"},
            "positions": 0,
            "open_orders": 0,
            "unresolved_intents": 0,
        }
        candidate = {
            "asset_class": "crypto", "candidate_ref": "crypto://BTC/USDC",
            "max_loss_usd": 10, "quote_age_seconds": 0, "spread_fraction": 0,
            "symbol": "BTC/USDC",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "result.json"
            result_path.write_text(json.dumps({
                "candidate_ref": "NO_TRADE", "probability_profit": 0,
                "expected_gain_usd": 0, "reason": "根拠不足",
            }))

            def completed(args, **kwargs):
                payload = json.loads(kwargs["input"].splitlines()[-1])
                self.assertEqual(payload["account"]["cash"], "0")
                self.assertEqual(payload["available_cash_usd"], "66.72")
                return subprocess.CompletedProcess(
                    args, 0, json.dumps({"result_path": str(result_path)}), "")

            with patch.object(allocator.subprocess, "run", side_effect=completed):
                allocator.choose(snapshot, [candidate], root / "state", Path("runner"), root)

    def _provider_snapshot(self, timestamp="2026-09-06T13:59:50Z", open_orders=0):
        clock = {"is_open": True, "timestamp": timestamp}
        with patch.object(alpaca_cli, "_context", return_value={}), patch.object(
            alpaca_cli, "_run", side_effect=[
                {"cash": "99980.01", "equity": "99980.01", "last_equity": "100000.00"},
                clock, [], [], [], [{"symbol": "SPY", "market_value": "89.99", "unrealized_pl": "-10.99"}],
                open_orders, {"price": "500", "timestamp": clock["timestamp"]},
                [{"symbol": "BTC/USD", "bid": "49999", "ask": "50000", "quote_at": clock["timestamp"]}],
                {"tradable": True, "status": "active", "overnight_tradable": True,
                 "overnight_halted": False},
                {"bid": "499", "ask": "500", "quote_at": clock["timestamp"]}, [],
            ]
        ):
            with tempfile.TemporaryDirectory() as directory:
                return alpaca_cli.read_allocator_snapshot(
                    credentials_path=Path("missing"), cli_path=Path("missing"),
                    risk_day_path=Path(directory) / "risk-day.json")

    def test_provider_snapshot_builds_the_fixed_risk_inputs(self):
        snapshot = self._provider_snapshot()
        self.assertEqual(snapshot["positions"], 1)
        self.assertFalse(snapshot["risk"]["risk_day_ready"])
        self.assertIsNone(snapshot["risk"]["official_pnl_ny_day_usd"])
        self.assertEqual(allocator.build_candidates(snapshot)[0]["max_loss_usd"], 10.0)

    def test_usdc_funding_is_cash_like_not_an_open_risk_position(self):
        clock = {"is_open": True, "timestamp": "2026-09-06T13:59:50Z"}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            alpaca_cli, "_context", return_value={}), patch.object(
                alpaca_cli, "_run", side_effect=[
                    {"cash": "0", "equity": "66.75", "last_equity": "0"}, clock, [],
                    [{"id": "deposit", "asset": "USDC", "usd_value": "66.75",
                      "direction": "INCOMING", "status": "COMPLETE"}], [],
                    [{"symbol": "USDCUSD", "market_value": "66.72", "unrealized_pl": "-0.03"}],
                    0, {"price": "500", "timestamp": clock["timestamp"]}, [],
                    {"tradable": True, "status": "active", "overnight_tradable": True,
                     "overnight_halted": False},
                    {"bid": "499", "ask": "500", "quote_at": clock["timestamp"]}, [],
                ]):
            snapshot = alpaca_cli.read_allocator_snapshot(
                credentials_path=Path("missing"), cli_path=Path("missing"),
                risk_day_path=Path(directory) / "risk-day.json")
        self.assertEqual(snapshot["positions"], 0)
        self.assertEqual(snapshot["available_cash_usd"], "66.72")
        self.assertEqual(snapshot["risk"]["allocated_capital_usd"], "0")

    def test_provider_nanoseconds_and_utc_offset_are_valid_risk_time(self):
        snapshot = self._provider_snapshot("2026-09-06T09:59:50.123456789-04:00")
        self.assertFalse(evaluate_entry(snapshot["risk"], "10.00", now=NOW)["approved"])

    def test_boolean_open_order_count_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "^alpaca_allocator_shape_invalid$"):
            self._provider_snapshot(open_orders=False)

    def test_exact_owner_caps_allow_only_below_or_at_limits(self):
        result = evaluate_entry(risk(equity_pnl_ny_day_usd="-10.00",
                                     official_pnl_ny_day_usd="-10.00"), "10.00", now=NOW)
        self.assertTrue(result["approved"])
        self.assertEqual(result["limits"], {"allocated_capital_usd": "100.00",
                                             "daily_loss_usd": "20.00",
                                             "trade_max_loss_usd": "10.00"})

    def test_each_cap_rejects_entry(self):
        cases = [
            (risk(), "10.01"),
            (risk(allocated_capital_usd="90.01"), "10.00"),
            (risk(equity_pnl_ny_day_usd="-20.00"), "10.00"),
            (risk(equity_pnl_ny_day_usd="-19.99", official_pnl_ny_day_usd="-19.99"), "10.00"),
        ]
        for snapshot, loss in cases:
            with self.subTest(snapshot=snapshot, loss=loss):
                self.assertFalse(evaluate_entry(snapshot, loss, now=NOW)["approved"])

    def test_unknown_nonfinite_stale_or_wrong_day_fail_closed(self):
        cases = [({}, "1"), (risk(cash_flow_ny_day_usd=None), "1"),
                 (risk(allocated_capital_usd=math.nan), "1"),
                 (risk(observed_at="2026-09-06T13:58:00Z"), "1"),
                 (risk(ny_day="2026-09-05"), "1")]
        for snapshot, loss in cases:
            with self.subTest(snapshot=snapshot):
                self.assertFalse(evaluate_entry(snapshot, loss, now=NOW)["approved"])

    def test_allocator_cannot_approve_when_fixed_contract_rejects(self):
        snapshot = {"account": {"cash": "100000", "equity": "100000"},
                    "clock": {"is_open": True}, "positions": 0, "open_orders": 0,
                    "risk": {}}
        candidate = {"asset_class": "crypto", "candidate_ref": "crypto://BTC/USD",
                     "max_loss_usd": "1", "quote_age_seconds": 0,
                     "spread_fraction": 0.01, "symbol": "BTC/USD"}
        decision = {"candidate_ref": candidate["candidate_ref"],
                    "probability_profit": 1, "expected_gain_usd": 1, "reason": "fixture"}
        gated = allocator.gate(snapshot, [candidate], decision)
        self.assertFalse(gated["approved"])
        self.assertEqual(gated["fixed_risk"]["gate"], "fixed_risk_rejected")

    def test_rejected_fixture_reaches_zero_broker_submits(self):
        allocator_snapshot = self._provider_snapshot()
        allocator_snapshot["risk"]["allocated_capital_usd"] = "90.01"
        candidate = {"asset_class": "crypto", "candidate_ref": "crypto://BTC/USD",
                     "max_loss_usd": "10", "quote_age_seconds": 0,
                     "spread_fraction": 0.01, "symbol": "BTC/USD"}
        rejected = allocator.gate(allocator_snapshot, [candidate], {
            "candidate_ref": candidate["candidate_ref"], "probability_profit": 1,
            "expected_gain_usd": 1, "reason": "fixture"})
        observation = {"account": {"cash": "100000", "equity": "100000"},
                       "activities_count": 0, "open_and_closed_orders_count": 0,
                       "positions": []}
        campaign = {"exit_status": "CLOSED", "unrealized_pnl_usd": "0.00"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(investment_run.os.environ, {
            "LIFE_MANAGER_INVESTMENT_MODE": "paper",
            "LIFE_MANAGER_INVESTMENT_DEPLOYMENT": "local",
            "ALPACA_INVESTMENT_STATE_DIR": directory,
        }), patch.object(investment_run, "reconcile_started", return_value={"pending": 0, "reconciled": 0, "unresolved": 0}), \
                patch.object(investment_run, "observe", return_value=observation), \
                patch.object(investment_run, "read_campaign_snapshot", return_value={}), \
                patch.object(investment_run, "reconcile", return_value=campaign), \
                patch.object(investment_run, "read_allocator_snapshot", return_value=allocator_snapshot), \
                patch.object(investment_run, "build_candidates", return_value=[candidate]), \
                patch.object(investment_run, "choose", return_value=rejected), \
                patch.object(investment_run, "_review_status", return_value={}), \
                patch.object(investment_run, "deliver", return_value={"message_id": "fixture"}), \
                patch.object(investment_run, "submit_order") as submit:
            self.assertEqual(investment_run.main(wake_id="risk-rejection"), 0)
        submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
