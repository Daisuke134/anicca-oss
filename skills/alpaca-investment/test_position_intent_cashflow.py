from datetime import datetime, timezone
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import allocator
import alpaca_cli
import effect_store


def snapshot(**changes):
    now = datetime.now(timezone.utc)
    value = {
        "account": {"cash": "100000", "equity": "100000"},
        "clock": {"is_open": True},
        "open_orders": 0,
        "positions": 0,
        "risk": {
            "allocated_capital_usd": "0",
            "cash_flow_ny_day_usd": "0",
            "equity_pnl_ny_day_usd": "0",
            "official_pnl_ny_day_usd": "0",
            "risk_day_ready": True,
            "unrealized_pnl_usd": "0",
            "observed_at": now.isoformat().replace("+00:00", "Z"),
            "ny_day": now.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
        },
        "unresolved_intents": 0,
    }
    value.update(changes)
    return value


CANDIDATE = {
    "asset_class": "crypto", "candidate_ref": "crypto://BTC/USD",
    "max_loss_usd": "10", "quote_age_seconds": 0,
    "spread_fraction": 0.01, "symbol": "BTC/USD",
}
DECISION = {
    "candidate_ref": "crypto://BTC/USD", "probability_profit": 0.9,
    "expected_gain_usd": 20, "reason": "fixture",
}


class OnePositionOneIntentTest(unittest.TestCase):
    def test_entry_requires_zero_positions_orders_and_unresolved_intents(self):
        self.assertTrue(allocator.gate(snapshot(), [CANDIDATE], DECISION)["approved"])
        for blocked in (
            {"positions": 1}, {"open_orders": 1}, {"unresolved_intents": 1},
        ):
            with self.subTest(blocked=blocked):
                result = allocator.gate(snapshot(**blocked), [CANDIDATE], DECISION)
                self.assertFalse(result["approved"])

    def test_restart_detects_multiple_unresolved_intents_before_broker_read(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "receipts.jsonl"
            for suffix in ("a", "b"):
                sealed = effect_store.seal(
                    ledger,
                    {"mode": "paper", "candidate_ref": suffix},
                    {"asset_class": "crypto", "symbol": "BTC/USD", "suffix": suffix},
                )
                effect_store.mark_started(ledger, sealed)
            restarted = importlib.reload(effect_store)
            calls = []
            with self.assertRaisesRegex(ValueError, "^multiple_unresolved_intents$"):
                restarted.reconcile_started(ledger, lambda value: calls.append(value))
            self.assertEqual(calls, [])

    def test_restart_recovers_applied_intent_when_outcome_write_was_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "receipts.jsonl"
            sealed = effect_store.seal(
                ledger, {"mode": "paper", "candidate_ref": "fixture"},
                {"asset_class": "crypto", "symbol": "BTC/USD"})
            effect_store.mark_started(ledger, sealed)
            with ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    **sealed, "paper": True, "receipt_type": "effect_intent",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "schema_version": 1, "status": "applied",
                }, separators=(",", ":")) + "\n")
            restarted = importlib.reload(effect_store)
            reads = []
            result = restarted.reconcile_started(
                ledger, lambda value: reads.append(value) or {
                    "client_order_id": value, "status": "filled"})
            self.assertEqual(result, {"pending": 1, "reconciled": 1, "unresolved": 0})
            self.assertEqual(reads, [sealed["client_order_id"]])


class CashFlowAdjustedPnlTest(unittest.TestCase):
    def test_completion_after_baseline_uses_usd_value_once_across_restart(self):
        observed = "2026-09-06T13:59:50Z"
        common = [
            {"is_open": True, "timestamp": observed},
            [
                {"activity_type": "CSD", "date": "2026-09-06", "net_amount": "100"},
                {"activity_type": "CSW", "date": "2026-09-06", "net_amount": "-40"},
            ],
            [{"symbol": "SPY", "market_value": "89.99", "unrealized_pl": "-9"}],
            0, {"price": "500", "timestamp": observed}, [],
            {"tradable": True, "status": "active"},
            {"bid": "499", "ask": "500", "quote_at": observed}, [],
        ]
        baseline = [{"id": "funding", "asset": "USDC", "usd_value": None,
                     "direction": "INCOMING", "status": "PROCESSING"}]
        complete = [{"id": "funding", "asset": "USDC", "usd_value": "9.99707",
                     "direction": "INCOMING", "status": "COMPLETE"}]
        responses = ([{"cash": "100000", "equity": "100000", "last_equity": "99900"}]
                     + common[:2] + [baseline] + common[2:]
                     + [{"cash": "100009.99707", "equity": "100009.99707", "last_equity": "99900"}]
                     + common[:2] + [complete] + common[2:] * 1
                     + [{"cash": "100009.99707", "equity": "100009.99707", "last_equity": "99900"}]
                     + common[:2] + [complete] + common[2:] * 1)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            alpaca_cli, "_context", return_value={}), patch.object(
                alpaca_cli, "_run", side_effect=responses) as read:
            path = Path(directory) / "risk-day.json"
            first = alpaca_cli.read_allocator_snapshot(
                credentials_path=Path("missing"), cli_path=Path("missing"), risk_day_path=path)
            second = alpaca_cli.read_allocator_snapshot(
                credentials_path=Path("missing"), cli_path=Path("missing"), risk_day_path=path)
            third = alpaca_cli.read_allocator_snapshot(
                credentials_path=Path("missing"), cli_path=Path("missing"), risk_day_path=path)
        self.assertFalse(first["risk"]["risk_day_ready"])
        self.assertEqual(second["risk"]["cash_flow_ny_day_usd"], "9.99707")
        self.assertEqual(second["risk"]["equity_pnl_ny_day_usd"], "0.00000")
        self.assertEqual(third["risk"]["cash_flow_ny_day_usd"], "9.99707")
        activity_args = read.call_args_list[2].args[1]
        self.assertIn("CSD,CSW", activity_args)
        self.assertIn("2026-09-06T04:00:00Z", activity_args)
        self.assertIn("2026-09-07T04:00:00Z", activity_args)
        self.assertEqual(read.call_args_list[3].args[1], [
            "api", "GET", "/v2/wallets/transfers", "--quiet", "--jq",
            "[.[]|{id,asset,usd_value,direction,status}]",
        ])


if __name__ == "__main__":
    unittest.main()
