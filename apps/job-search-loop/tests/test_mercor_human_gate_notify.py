import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search_loop.mercor_human_gate_notify import record_and_notify


class MercorHumanGateNotifyTests(unittest.TestCase):
    def test_same_human_gate_uses_one_stable_notification_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "telegram.env"
            env.write_text("JOB_SEARCH_TELEGRAM_CHAT_ID=123\n", encoding="utf-8")
            calls = []
            notification = type("Notification", (), {})()
            def notify_effect(**kwargs):
                calls.append(kwargs["event_key"])
                return {"delivery": "delivered", "provider_message_id": "tg-1", "attempted": 1}
            notification.notify_effect = notify_effect
            with patch(
                "job_search_loop.mercor_human_gate_notify._load_notification",
                return_value=notification,
            ):
                arguments = dict(
                    gate_store=root / "gates.jsonl", outbox=root / "outbox.sqlite3",
                    telegram_env=env, listing_id="list-jp", title="Japanese Writer",
                    reason="Finance Interview must be completed",
                    evidence_ref="https://work.mercor.com/explore?listingId=list-jp",
                )
                first = record_and_notify(run_id="run-1", **arguments)
                second = record_and_notify(run_id="run-2", **arguments)
            self.assertEqual(first["gate_id"], second["gate_id"])
            self.assertEqual(calls[0], calls[1])


if __name__ == "__main__":
    unittest.main()
