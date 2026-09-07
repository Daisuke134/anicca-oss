import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("token-daily-report.sh")


class TokenDailyReportContractTest(unittest.TestCase):
    def test_report_is_one_exact_jst_day_all_agents_and_pinned_tool(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("ccusage@20.0.18 daily", text)
        self.assertIn('--since "$YESTERDAY"', text)
        self.assertIn('--until "$YESTERDAY"', text)
        self.assertIn("--timezone Asia/Tokyo", text)
        self.assertIn("--by-agent", text)
        self.assertNotIn("ccusage@latest", text)
        self.assertIn("API換算", text)

    def test_report_includes_loop_attributed_runner_telemetry(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("usage_report.py", text)
        self.assertIn("AGENT_USAGE_REPORT_BIN", text)
        self.assertNotIn("profitable-claude", text)
        self.assertIn('--date "$YESTERDAY_ISO"', text)
        self.assertIn("loop別runner実測", text)

    def test_report_uses_repository_owned_telegram_sender(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('../_shared/send-telegram.sh', text)
        self.assertNotIn("openclaw message send", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
