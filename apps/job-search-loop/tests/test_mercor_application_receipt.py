import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search_loop.mercor_application_receipt import record_and_report


class MercorApplicationReceiptTests(unittest.TestCase):
    def test_official_readback_records_and_reports_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            screenshot = evidence_root / "submitted.png"
            screenshot.write_bytes(b"png")
            evidence = evidence_root / "readback.json"
            evidence.write_text(json.dumps({
                "page_url": "https://work.mercor.com/jobs/apply/candidate-x",
                "visible_text": "Your application has been submitted!",
                "screenshot_path": str(screenshot),
            }), encoding="utf-8")
            telegram_env = root / "telegram.env"
            telegram_env.write_text(
                "TELEGRAM_BOT_TOKEN=test\nJOB_SEARCH_TELEGRAM_CHAT_ID=123\n",
                encoding="utf-8",
            )

            with patch(
                "job_search_loop.mercor_application_receipt._load"
            ) as load:
                notification = type("Notification", (), {})()
                notification.notify_effect = lambda **kwargs: {
                    "event_key": kwargs["event_key"],
                    "delivery": "delivered",
                    "provider_message_id": "tg-1",
                    "attempted": 1,
                }
                load.return_value = notification

                first = record_and_report(
                    state_root=root / "state", outbox_database=root / "outbox.sqlite3",
                    telegram_env=telegram_env, listing_id="list-new", title="Japanese Writer",
                    url="https://work.mercor.com/explore?listingId=list-new", run_id="run-1",
                    readback_evidence=evidence, evidence_root=evidence_root,
                    now="2026-09-07T05:00:00+00:00",
                )
                replay = record_and_report(
                    state_root=root / "state", outbox_database=root / "outbox.sqlite3",
                    telegram_env=telegram_env, listing_id="list-new", title="Japanese Writer",
                    url="https://work.mercor.com/explore?listingId=list-new", run_id="run-2",
                    readback_evidence=evidence, evidence_root=evidence_root,
                    now="2026-09-07T05:01:00+00:00",
                )

            self.assertTrue(first["recorded"])
            self.assertFalse(replay["recorded"])
            self.assertEqual(first["provider_message_id"], "tg-1")
            self.assertEqual(replay["provider_message_id"], "tg-1")
            self.assertEqual(len((root / "state/applications.jsonl").read_text().splitlines()), 1)

    def test_ambiguous_readback_fails_before_record_or_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            screenshot = root / "submitted.png"
            screenshot.write_bytes(b"png")
            evidence = root / "readback.json"
            evidence.write_text(json.dumps({
                "page_url": "https://work.mercor.com/jobs/apply/candidate-x",
                "visible_text": "Loading...",
                "screenshot_path": str(screenshot),
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "official_readback_unverified"):
                record_and_report(
                    state_root=root / "state", outbox_database=root / "outbox.sqlite3",
                    telegram_env=root / "telegram.env", listing_id="list-new", title="Role",
                    url="https://work.mercor.com/explore?listingId=list-new", run_id="run-1",
                    readback_evidence=evidence, evidence_root=root,
                )
            self.assertFalse((root / "state/applications.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
