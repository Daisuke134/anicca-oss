import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import reporter


OBSERVATION = {
    "clock": {"observed_at": "2026-09-04T10:10:25Z"},
    "account": {
        "account_id": "must-not-appear",
        "equity": "99996.76",
        "cash": "99996.76",
    },
    "positions": [],
}
CAMPAIGN = {"realized_pnl_usd": "-3.00", "unrealized_pnl_usd": "0.00"}


class ModeReportTest(unittest.TestCase):
    def test_success_and_failure_reports_expose_mode(self):
        message = reporter.render(
            {"account": {"equity": "100000", "cash": "100000"}, "positions": []},
            {"unrealized_pnl_usd": "0.00"},
            {"candidate_ref": "NO_TRADE", "gate": "model_no_trade",
             "observed_at": "2026-09-05T00:00:00Z", "mode": "shadow",
             "application_status": "in_review", "risk": {
                 "equity_pnl_ny_day_usd": "-1", "official_pnl_ny_day_usd": "-2",
                 "unrealized_pnl_usd": "-0.50"}}, "none")
        self.assertIn("モード: shadow", message)
        self.assertIn("ライブ口座: 審査中", message)
        self.assertTrue(message.startswith("[Investment Loop][投資判断]"))
        self.assertNotIn("Codex", message)
        self.assertNotIn("Alpaca", message)
        self.assertIn("理由: 理由は記録されていません", message)
        self.assertIn("日次純損益: -$2.00", message)
        self.assertIn("含み損益: -$0.50", message)
        self.assertIn("残り日次損失枠: $18.00", message)
        self.assertIn("次回確認: 2026-09-05T00:05:00+00:00", message)
        self.assertNotIn("開始時$100,000", message)
        failure = reporter.render_failure(
            stage="observe", effect_uncertain=False,
            wake_id="2026-09-05T00:00:00Z", mode="live")
        self.assertIn("モード: live", failure)
        self.assertTrue(failure.startswith("[Investment Loop][実行エラー]"))
        self.assertNotIn("Codex", failure)
        self.assertNotIn("Alpaca", failure)
        self.assertIn("live注文", failure)
        self.assertNotIn("paper注文", failure)

    def test_unknown_live_pnl_is_reported_unknown_not_zero(self):
        message = reporter.render(
            {"account": {"equity": "66.75", "cash": "0"}, "positions": [{}]},
            {}, {"candidate_ref": "NO_TRADE", "gate": "risk_rejected",
                 "reason": "daily evidence unknown", "observed_at": "bad-time",
                 "mode": "live", "application_status": "active", "risk": {
                     "equity_pnl_ny_day_usd": "0", "official_pnl_ny_day_usd": None,
                     "unrealized_pnl_usd": "-0.01"}}, "none")
        self.assertIn("日次純損益: 不明", message)
        self.assertIn("残り日次損失枠: 不明", message)
        self.assertIn("次回確認: 不明", message)

    def test_live_account_and_nanosecond_next_wake_are_readable(self):
        message = reporter.render(
            {"account": {"equity": "66.72", "cash": "0", "status": "ACTIVE"},
             "positions": [{}]}, {},
            {"candidate_ref": "NO_TRADE", "gate": "model_no_trade", "reason": "見送り",
             "observed_at": "2026-09-09T10:50:26.139497622-04:00", "mode": "shadow",
             "risk": {"equity_pnl_ny_day_usd": "0",
                      "official_pnl_ny_day_usd": "-0.02", "unrealized_pnl_usd": "-0.02"}},
            "none")
        self.assertIn("ライブ口座: 有効", message)
        self.assertIn("次回確認: 2026-09-09T14:55:26.139497+00:00", message)


class FailureBalanceTest(unittest.TestCase):
    def test_failure_report_reads_last_snapshot_and_names_it_as_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "observation-latest.json").write_text(json.dumps(OBSERVATION))
            (state / "campaign.json").write_text(json.dumps(CAMPAIGN))
            with patch.object(
                reporter,
                "_deliver_message",
                return_value={"message_id": "123", "status": "delivered"},
            ) as send:
                reporter.deliver_failure(
                    state,
                    stage="observe",
                    effect_uncertain=False,
                    wake_id="2026-09-04T10:15:00Z",
                    mode="paper",
                )

        message = send.call_args.args[2]
        self.assertIn("利用可能な最新値", message)
        self.assertIn("資産は $99,996.76", message)
        self.assertIn("現金は $99,996.76", message)
        self.assertIn("開始時$100,000から -$3.24", message)
        self.assertIn("確定損益 -$3.00", message)
        self.assertIn("含み損益 $0.00", message)
        self.assertIn("保有ポジション 0件", message)
        self.assertIn("2026-09-04T10:10:25Z", message)
        self.assertNotIn("must-not-appear", message)

    def test_nonpaper_failure_omits_paper_baseline(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            reporter,
            "_deliver_message",
            return_value={"message_id": "123", "status": "delivered"},
        ) as send:
            reporter.deliver_failure(
                Path(directory),
                stage="observe",
                effect_uncertain=False,
                wake_id="2026-09-04T10:15:00Z",
                observation=OBSERVATION,
                campaign=CAMPAIGN,
                mode="live",
            )
        message = send.call_args.args[2]
        self.assertIn("資産は $99,996.76", message)
        self.assertNotIn("開始時$100,000", message)

    def test_partial_or_malformed_snapshot_reports_each_unknown_without_crashing(self):
        observation = {
            "clock": "malformed",
            "account": {"equity": "NaN", "account_id": "must-not-appear"},
        }
        with tempfile.TemporaryDirectory() as directory, patch.object(
            reporter,
            "_deliver_message",
            return_value={"message_id": "123", "status": "delivered"},
        ) as send:
            result = reporter.deliver_failure(
                Path(directory),
                stage="observe",
                effect_uncertain=False,
                wake_id="2026-09-04T10:15:00Z",
                observation=observation,
                campaign={"realized_pnl_usd": "-3.00"},
            )

        self.assertEqual(result["status"], "delivered")
        message = send.call_args.args[2]
        self.assertIn("資産は 不明", message)
        self.assertIn("現金は 不明", message)
        self.assertNotIn("開始時$100,000", message)
        self.assertIn("確定損益 -$3.00", message)
        self.assertIn("含み損益 不明", message)
        self.assertIn("保有ポジション 不明", message)
        self.assertIn("観測時刻 不明", message)
        self.assertNotIn("must-not-appear", message)


if __name__ == "__main__":
    unittest.main()
