from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_status


class ReviewStatusTest(unittest.TestCase):
    def test_classifies_only_explicit_provider_states(self):
        self.assertEqual(review_status.classify_dashboard("Application submitted: In review"), "in_review")
        self.assertEqual(review_status.classify_dashboard("Action required to finish your application"), "action_required")
        self.assertEqual(review_status.classify_dashboard("Application rejected"), "rejected")
        self.assertEqual(review_status.classify_dashboard("", "Life Manager\nLive - ABC12345"), "active")
        self.assertEqual(review_status.classify_dashboard("", "Individual Trading"), "active")
        self.assertIsNone(review_status.classify_dashboard("Welcome to Alpaca"))

    def test_live_candidate_in_mixed_paper_dom_is_not_active(self):
        body = "Life Manager\nPaper - PA123456\nLive - ABC12345"
        self.assertIsNone(review_status.classify_dashboard(body, "Life Manager\nPaper - PA123456"))

    def test_dashboard_shell_is_not_ready_before_provider_state_renders(self):
        url = "https://app.alpaca.markets/dashboard/overview"
        self.assertFalse(review_status.dashboard_ready(url, "Home Account API Community Support"))
        self.assertFalse(review_status.dashboard_ready(url, "", "Loading..."))
        self.assertTrue(review_status.dashboard_ready(url, "", "Life Manager\nPaper - PA123456"))
        self.assertTrue(review_status.dashboard_ready(url, "Application submitted: In review"))
        self.assertTrue(review_status.dashboard_ready("https://app.alpaca.markets/login", ""))

    def test_account_switcher_expression_supports_current_visible_button(self):
        self.assertIn('document.querySelectorAll("button")', review_status.ACCOUNT_SWITCHER)
        self.assertIn("Paper|Live", review_status.ACCOUNT_SWITCHER)
        self.assertIn("Individual Trading", review_status.ACCOUNT_SWITCHER)

    def test_due_uses_last_provider_observation(self):
        now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(review_status.due({}, now=now))
        self.assertFalse(review_status.due(
            {"observed_at": "2026-09-05T11:45:00Z"}, now=now, interval_seconds=1800
        ))
        self.assertTrue(review_status.due(
            {"observed_at": "2026-09-05T11:29:59Z"}, now=now, interval_seconds=1800
        ))

    def test_unrecognized_dashboard_keeps_prior_receipt_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            receipt = state / "account-status.json"
            receipt.write_text('{"application_status":"in_review","observed_at":"2026-09-05T00:00:00Z"}\n')
            receipt.chmod(0o600)
            calls = []

            def command(args, **kwargs):
                calls.append(args)
                if "ensure_browser.sh" in " ".join(args):
                    return "ALIVE"
                if "acquire" in args:
                    return '{"target_id":"t1","token":"tok","generation":1}'
                if "eval" in args:
                    return '{"url":"https://app.alpaca.markets/dashboard/overview","text":"Welcome","selected":""}'
                return "OK"

            with patch.object(review_status, "_command", side_effect=command), \
                    patch.object(review_status.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "review_status_unrecognized"):
                    review_status.refresh(state, force=True)

            self.assertEqual(review_status.read_receipt(state)["application_status"], "in_review")
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertTrue(any("release" in args for args in calls))

    def test_active_requires_live_text_on_same_account_switcher_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "account-status.json").write_text(
                '{"application_status":"in_review","observed_at":"2026-09-05T00:00:00Z"}\n'
            )
            snapshots = iter((
                '{"url":"https://app.alpaca.markets/dashboard/overview","text":"Live - CANDIDATE123","selected":"Life Manager\\nPaper - PAPER123"}',
                "true",
                "true",
                '{"url":"https://app.alpaca.markets/dashboard/overview","text":"Live - CANDIDATE123","selected":"Life Manager\\nLive - LIVE1234"}',
            ))
            eval_inputs = []

            def command(args, **kwargs):
                if "ensure_browser.sh" in " ".join(args):
                    return "ALIVE"
                if "acquire" in args:
                    return '{"target_id":"t1","token":"tok","generation":1}'
                if "eval" in args:
                    eval_inputs.append(kwargs.get("stdin", ""))
                    return next(snapshots)
                return "OK"

            with patch.object(review_status, "_command", side_effect=command), \
                    patch.object(review_status.time, "sleep"):
                result = review_status.refresh(state, force=True)

            self.assertEqual(result["application_status"], "active")
            self.assertTrue(all("nav > div.h-14 > button" in value for value in (eval_inputs[0], eval_inputs[1])))
            self.assertEqual(review_status.read_receipt(state)["application_status"], "active")


if __name__ == "__main__":
    unittest.main()
