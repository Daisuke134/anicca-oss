from datetime import datetime, timezone
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import risk_day


NOW = datetime(2026, 9, 9, 14, 0, tzinfo=timezone.utc)


def transfer(identifier="t1", *, status="COMPLETE", direction="INCOMING",
             usd_value="10"):
    return {"id": identifier, "asset": "USDC", "direction": direction,
            "status": status, "usd_value": usd_value}


class RiskDayTest(unittest.TestCase):
    def test_existing_complete_transfer_is_baseline_not_profit(self):
        with tempfile.TemporaryDirectory() as directory:
            result = risk_day.reconcile(
                Path(directory) / "risk-day.json", observed_at=NOW, equity="66.75",
                bank_cash_flow="0", transfers=[transfer(usd_value="66.75055405")],
                trade_activity_ids=[], official_unrealized="-0.01")
        self.assertEqual(result["cash_flow_ny_day_usd"], "0")
        self.assertEqual(result["equity_pnl_ny_day_usd"], "0")
        self.assertFalse(result["risk_day_ready"])

    def test_outgoing_completion_is_negative_and_counted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk-day.json"
            pending = transfer(status="PROCESSING", direction="OUTGOING", usd_value=None)
            risk_day.reconcile(path, observed_at=NOW, equity="100", bank_cash_flow="0",
                               transfers=[pending], trade_activity_ids=[], official_unrealized="0")
            complete = transfer(direction="OUTGOING", usd_value="7.25")
            first = risk_day.reconcile(path, observed_at=NOW, equity="92.75", bank_cash_flow="0",
                                       transfers=[complete], trade_activity_ids=[],
                                       official_unrealized="0")
            second = risk_day.reconcile(path, observed_at=NOW, equity="92.75", bank_cash_flow="0",
                                        transfers=[complete], trade_activity_ids=[],
                                        official_unrealized="0")
        self.assertEqual(first["cash_flow_ny_day_usd"], "-7.25")
        self.assertEqual(first["equity_pnl_ny_day_usd"], "0.00")
        self.assertEqual(second["cash_flow_ny_day_usd"], "-7.25")

    def test_new_day_rebaselines_and_excludes_prior_day_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk-day.json"
            risk_day.reconcile(path, observed_at=NOW, equity="100", bank_cash_flow="60",
                               transfers=[], trade_activity_ids=[], official_unrealized="0")
            tomorrow = datetime(2026, 9, 10, 14, 0, tzinfo=timezone.utc)
            result = risk_day.reconcile(path, observed_at=tomorrow, equity="110",
                                        bank_cash_flow="10", transfers=[],
                                        trade_activity_ids=[], official_unrealized="0")
            saved = json.loads(path.read_text())
        self.assertEqual(result["cash_flow_ny_day_usd"], "0")
        self.assertEqual(saved["ny_day"], "2026-09-10")

    def test_malformed_or_duplicate_transfer_fails_closed(self):
        cases = ([transfer(), transfer()], [transfer(usd_value=None)],
                 [{"id": "t1", "asset": "USDC", "direction": "SIDEWAYS",
                   "status": "COMPLETE", "usd_value": "10"}])
        for rows in cases:
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    risk_day.reconcile(Path(directory) / "risk-day.json", observed_at=NOW,
                                       equity="100", bank_cash_flow="0", transfers=rows,
                                       trade_activity_ids=[], official_unrealized="0")

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk-day.json"
            path.write_text("not-json")
            with self.assertRaisesRegex(ValueError, "^risk_day_invalid$"):
                risk_day.reconcile(path, observed_at=NOW, equity="100",
                                   bank_cash_flow="0", transfers=[], trade_activity_ids=[],
                                   official_unrealized="0")

    def test_new_fill_makes_official_pnl_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk-day.json"
            risk_day.reconcile(path, observed_at=NOW, equity="100", bank_cash_flow="0",
                               transfers=[], trade_activity_ids=[], official_unrealized="0")
            known = risk_day.reconcile(path, observed_at=NOW, equity="99", bank_cash_flow="0",
                                       transfers=[], trade_activity_ids=[],
                                       official_unrealized="-1")
            unknown = risk_day.reconcile(path, observed_at=NOW, equity="99", bank_cash_flow="0",
                                         transfers=[], trade_activity_ids=["fill-1"],
                                         official_unrealized="-1")
        self.assertTrue(known["risk_day_ready"])
        self.assertEqual(known["official_pnl_ny_day_usd"], "-1")
        self.assertFalse(unknown["risk_day_ready"])
        self.assertIsNone(unknown["official_pnl_ny_day_usd"])

    def test_day_first_seen_after_a_fill_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "risk-day.json"
            for _ in range(2):
                result = risk_day.reconcile(
                    path, observed_at=NOW, equity="100", bank_cash_flow="0", transfers=[],
                    trade_activity_ids=["earlier-fill"], official_unrealized="0")
        self.assertFalse(result["risk_day_ready"])
        self.assertIsNone(result["official_pnl_ny_day_usd"])


if __name__ == "__main__":
    unittest.main()
