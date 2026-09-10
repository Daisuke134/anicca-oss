import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import repeatability


class RepeatabilityTest(unittest.TestCase):
    def fixture(self, root: Path, wakes: int, days: int):
        shadow = root / "shadow"; live = root / "live"
        shadow.mkdir(); live.mkdir()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = []
        for index in range(wakes):
            at = start + timedelta(days=index % days, seconds=index * 2)
            run = f"run-{index}-{100 + index % 3}"
            events.append({"event_id": f"execute-{index}", "run_id": run,
                           "phase": "execute", "status": "running", "timestamp": at.isoformat(),
                           "effect_class": "none", "effect_status": "not_applicable"})
            events.append({"event_id": f"report-{index}", "run_id": run,
                           "phase": "report", "status": "pass",
                           "timestamp": (at + timedelta(seconds=1)).isoformat(),
                           "effect_class": "none", "effect_status": "not_applicable"})
        (shadow / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
        database = sqlite3.connect(shadow / "telegram-outbox.sqlite3")
        database.execute("CREATE TABLE telegram_outbox (event_key TEXT PRIMARY KEY,"
                         "message_sha256 TEXT NOT NULL,message TEXT NOT NULL,status TEXT NOT NULL,"
                         "attempt_count INTEGER NOT NULL,provider_message_id TEXT,created_at TEXT NOT NULL,"
                         "claimed_at TEXT,delivered_at TEXT,last_error_code TEXT)")
        for index in range(wakes):
            at = start + timedelta(days=index % days, seconds=index * 2)
            database.execute("INSERT INTO telegram_outbox VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (f"alpaca-wake:{index}", "hash", "fixture", "delivered", 1, str(index),
                              at.isoformat(), None, (at + timedelta(seconds=1)).isoformat(), None))
        database.commit(); database.close()
        return shadow, live, start

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_pass_requires_30_days_100_wakes_and_delivery(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 100, 30)
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertEqual(result["status"], "pass")
        self.assertTrue(all(result["checks"].values()))

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_collecting_exposes_missing_time_and_wakes(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 5, 1)
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertEqual(result["status"], "collecting")
        self.assertFalse(result["checks"]["calendar_days"])
        self.assertFalse(result["checks"]["natural_wakes"])

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_one_hundred_wakes_pass_without_thirty_days(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 100, 1)
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["checks"]["repeatability_window"])
        self.assertFalse(result["checks"]["calendar_days"])

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_thirty_days_with_weekend_pass_without_one_hundred_wakes(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 30, 30)
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["checks"]["repeatability_window"])
        self.assertFalse(result["checks"]["natural_wakes"])

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 1, "duplicate_order_ids": 0})
    def test_duplicate_official_client_id_fails(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 100, 30)
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertFalse(result["checks"]["official_duplicates_zero"])

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_missing_telegram_delivery_fails(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 100, 30)
            database = sqlite3.connect(shadow / "telegram-outbox.sqlite3")
            database.execute("DELETE FROM telegram_outbox WHERE event_key='alpaca-wake:99'")
            database.commit(); database.close()
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertFalse(result["checks"]["telegram_every_wake"])

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_delivered_failure_report_counts_for_failed_wake(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 1, 1)
            events = [json.loads(line) for line in
                      (shadow / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            events[-1]["status"] = "fail"
            (shadow / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
            database = sqlite3.connect(shadow / "telegram-outbox.sqlite3")
            database.execute("UPDATE telegram_outbox SET event_key='alpaca-failure:0' "
                             "WHERE event_key='alpaca-wake:0'")
            database.commit(); database.close()
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertEqual(result["observed"]["delivered_reports"], 1)
        self.assertTrue(result["checks"]["telegram_every_wake"])

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_two_deliveries_for_one_wake_fail(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 1, 1)
            database = sqlite3.connect(shadow / "telegram-outbox.sqlite3")
            database.execute("INSERT INTO telegram_outbox VALUES (?,?,?,?,?,?,?,?,?,?)",
                             ("alpaca-failure:duplicate", "hash2", "fixture2", "delivered",
                              1, "duplicate", (start + timedelta(milliseconds=500)).isoformat(),
                              None, (start + timedelta(seconds=1)).isoformat(), None))
            database.commit(); database.close()
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertFalse(result["checks"]["telegram_every_wake"])


if __name__ == "__main__":
    unittest.main()
