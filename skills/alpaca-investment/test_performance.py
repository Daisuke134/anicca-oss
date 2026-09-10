import copy
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import performance
import performance_gate
import alpaca_cli


class NetPerformanceTest(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(
            (ROOT / "fixtures/net-performance.json").read_text(encoding="utf-8"))

    def test_owner_deposit_is_not_profit_and_costs_are_explicit(self):
        result = performance.project(self.fixture)
        self.assertEqual(result["measurement_status"], "measured")
        self.assertEqual(result["net_pnl_usd"], "10.00")
        self.assertEqual(result["realized_pnl_usd"], "8.00")
        self.assertEqual(result["unrealized_pnl_usd"], "2.00")
        self.assertEqual(result["gross_strategy_pnl_usd"], "13.00")
        self.assertEqual(result["fees_usd"], "2.00")
        self.assertEqual(result["slippage_usd"], "1.00")
        self.assertEqual(result["observed_endpoint_drawdown_usd"], "0.00")
        self.assertEqual(result["drawdown_scope"], "official_period_endpoints")
        self.assertEqual(result["drawdown_observation_count"], 2)
        self.assertEqual(result["gross_exposure_usd"], "40.00")
        self.assertEqual(result["benchmark_pnl_usd"], "2.50")
        self.assertEqual(result["alpha_pnl_usd"], "7.50")
        self.assertFalse(result["capital_expansion_allowed"])
        self.assertFalse(result["statistically_supported"])
        self.assertEqual(result["capital_cap_usd"], "100.00")

    def test_projection_is_identical_after_module_restart(self):
        first = performance.project(self.fixture)
        restarted = importlib.reload(performance)
        self.assertEqual(restarted.project(self.fixture), first)

    def test_every_required_unknown_blocks_capital_expansion(self):
        for key in sorted(performance.REQUIRED):
            with self.subTest(key=key):
                incomplete = copy.deepcopy(self.fixture)
                incomplete.pop(key)
                result = performance.project(incomplete)
                self.assertEqual(result["measurement_status"], "blocked")
                self.assertFalse(result["capital_expansion_allowed"])
                self.assertNotIn("net_pnl_usd", result)

    def test_live_gate_extracts_exact_sealed_round_trip(self):
        rows = [
            {"receipt_type": "decision", "decision_id": "buy-decision",
             "recorded_at": "2026-09-09T15:26:26Z",
             "decision": {"canary_ref": "L09_LOCAL_CANARY_V1"}},
            {"receipt_type": "effect_intent", "decision_id": "buy-decision",
             "status": "planned", "client_order_id": "lm-ai-" + "a" * 24},
            {"receipt_type": "decision", "decision_id": "sell-decision",
             "recorded_at": "2026-09-09T16:10:39Z",
             "decision": {"close_ref": "L10_LOCAL_CLOSE_V1"}},
            {"receipt_type": "effect_intent", "decision_id": "sell-decision",
             "status": "planned", "client_order_id": "lm-ai-" + "b" * 24},
        ]
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "receipts.jsonl"
            ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertEqual(performance_gate._round_trip(ledger), (
                "2026-09-09T15:26:26Z", "lm-ai-" + "a" * 24, "lm-ai-" + "b" * 24))

    def test_live_gate_rejects_duplicate_canary_decision(self):
        row = {"receipt_type": "decision", "decision_id": "buy",
               "recorded_at": "2026-09-09T15:26:26Z",
               "decision": {"canary_ref": "L09_LOCAL_CANARY_V1"}}
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "receipts.jsonl"
            ledger.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
            with self.assertRaisesRegex(ValueError, "live_performance_round_trip_invalid"):
                performance_gate._round_trip(ledger)

    def test_official_adapter_separates_cashflow_realized_and_slippage(self):
        buy_client, sell_client = "lm-ai-" + "a" * 24, "lm-ai-" + "b" * 24
        buy_order, sell_order = "buy-order", "sell-order"
        responses = [
            {"cash": "0", "equity": "99"},
            {"timestamp": "2026-09-10T00:00:00Z"},
            [{"symbol": "USDCUSD", "qty": "99", "market_value": "99",
              "unrealized_pl": "0", "current_price": "1"}],
            [{"id": "transfer", "asset": "USDC", "amount": "100", "usd_value": "100",
              "direction": "INCOMING", "status": "COMPLETE",
              "created_at": "2026-09-09T00:00:00Z"}],
            [{"id": buy_order, "client_order_id": buy_client, "status": "filled",
              "symbol": "BTC/USDC", "side": "buy", "filled_qty": "1"},
             {"id": sell_order, "client_order_id": sell_client, "status": "filled",
              "symbol": "BTC/USDC", "side": "sell", "filled_qty": "1"}],
            [{"id": "buy-fill", "activity_type": "FILL", "order_id": buy_order,
              "symbol": "BTC/USDC", "side": "buy", "qty": "1", "price": "10",
              "transaction_time": "2026-09-09T00:01:00Z"},
             {"id": "sell-fill", "activity_type": "FILL", "order_id": sell_order,
              "symbol": "BTC/USDC", "side": "sell", "qty": "1", "price": "9",
              "transaction_time": "2026-09-09T00:02:00Z"}],
            [{"id": "buy-fee", "activity_type": "CFEE", "order_id": buy_order,
              "symbol": "BTCUSD", "qty": "-0.01", "price": "10", "date": "2026-09-09"},
             {"id": "sell-fee", "activity_type": "CFEE", "order_id": sell_order,
              "symbol": "USDCUSD", "qty": "-0.1", "price": "1", "date": "2026-09-09"}],
            [{"t": "2026-09-09T00:00:01Z", "bp": "9", "ap": "11"}],
            [{"t": "2026-09-09T00:01:00Z", "bp": "9", "ap": "10"}],
            [{"t": "2026-09-09T00:02:00Z", "bp": "9", "ap": "10"}],
            {"t": "2026-09-09T23:59:59Z", "bp": "10", "ap": "12"},
            {"timestamp": "2026-09-10T00:00:00Z"},
        ]
        with patch.object(alpaca_cli, "_context", return_value={}), patch.object(
                alpaca_cli, "_run", side_effect=responses):
            snapshot = alpaca_cli.read_live_performance_snapshot(
                credentials_path=Path("credentials"), cli_path=Path("alpaca"),
                period_start="2026-09-09T00:00:00Z",
                buy_client_order_id=buy_client, sell_client_order_id=sell_client)
        self.assertEqual(snapshot["owner_cash_flow_usd"], "100")
        self.assertEqual(snapshot["realized_pnl_usd"], "-1")
        self.assertEqual(snapshot["slippage_usd"], "0")
        self.assertEqual(snapshot["fees_usd"], "0.20")
        projected = performance.project(snapshot)
        self.assertEqual(projected["net_pnl_usd"], "-1.00")
        self.assertEqual(projected["observed_endpoint_drawdown_usd"], "1.00")

    def test_non_finite_negative_cost_and_impossible_peak_fail_closed(self):
        cases = (
            {"ending_nav_usd": "NaN"},
            {"fees_usd": "-0.01"},
            {"slippage_usd": "Infinity"},
            {"benchmark_start_price_usd": "0"},
            {"observed_at": "not-a-time"},
            {"period_start": "2026-09-08T00:00:00Z"},
            {"completed_round_trips": -1},
            {"completed_round_trips": True},
            {"realized_pnl_usd": "7.98"},
            {"owner_cash_flow_usd": "100.01"},
            {"gross_exposure_usd": "100.01"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                result = performance.project({**self.fixture, **changes})
                self.assertEqual(result["measurement_status"], "blocked")
                self.assertFalse(result["capital_expansion_allowed"])


if __name__ == "__main__":
    unittest.main()
