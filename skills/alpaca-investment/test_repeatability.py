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
    def fixture(self, root: Path, wakes: int, days: int, live_wakes: int = 0):
        shadow = root / "shadow"; live = root / "live"
        shadow.mkdir(); live.mkdir()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for state, count, offset, effect_class, effect_status in (
                (shadow, wakes, 0, "none", "not_applicable"),
                (live, live_wakes, wakes, "money", "unknown")):
            events = []
            database = sqlite3.connect(state / "telegram-outbox.sqlite3")
            database.execute("CREATE TABLE telegram_outbox (event_key TEXT PRIMARY KEY,"
                             "message_sha256 TEXT NOT NULL,message TEXT NOT NULL,status TEXT NOT NULL,"
                             "attempt_count INTEGER NOT NULL,provider_message_id TEXT,created_at TEXT NOT NULL,"
                             "claimed_at TEXT,delivered_at TEXT,last_error_code TEXT)")
            for local_index in range(count):
                index = offset + local_index
                at = start + timedelta(days=index % days, seconds=index * 2)
                run = f"run-{index}-{100 + index % 3}"
                events.append({"event_id": f"execute-{index}", "run_id": run,
                               "phase": "execute", "status": "running", "timestamp": at.isoformat(),
                               "provider": "shared-agent-runner",
                               "effect_class": effect_class, "effect_status": effect_status})
                events.append({"event_id": f"report-{index}", "run_id": run,
                               "phase": "report", "status": "pass",
                               "timestamp": (at + timedelta(seconds=1)).isoformat(),
                               "provider": "shared-agent-runner",
                               "effect_class": effect_class, "effect_status": effect_status})
                database.execute("INSERT INTO telegram_outbox VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (f"alpaca-wake:{index}", "hash", "fixture", "delivered", 1,
                                  str(index), at.isoformat(), None,
                                  (at + timedelta(seconds=1)).isoformat(), None))
            (state / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
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
    def test_shadow_and_live_wakes_complete_one_window(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 86, 2, live_wakes=14)
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["observed"]["natural_wakes"], 100)
        self.assertEqual(result["observed"]["shadow_wakes"], 86)
        self.assertEqual(result["observed"]["live_wakes"], 14)
        self.assertEqual(result["observed"]["delivered_reports"], 100)
        self.assertEqual(result["observed"]["unreported_wakes"], 0)
        self.assertTrue(result["checks"]["shadow_no_effect"])

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_inner_provider_report_is_not_a_natural_wake(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 1, 1, live_wakes=1)
            with (live / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event_id": "inner", "run_id": "inner-run",
                    "phase": "report", "status": "pass", "provider": "codex",
                    "timestamp": (start + timedelta(seconds=10)).isoformat(),
                    "effect_class": "money", "effect_status": "unknown"}) + "\n")
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, required_days=30,
                required_wakes=100, credentials=Path("c"), cli=Path("a"))
        self.assertEqual(result["observed"]["natural_wakes"], 2)
        self.assertTrue(result["checks"]["one_terminal_per_run"])

    @patch.object(repeatability, "_official_orders", return_value={
        "count": 2, "duplicate_client_ids": 0, "duplicate_order_ids": 0})
    def test_explicit_transition_freezes_shadow_then_counts_live(self, _official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 3, 1, live_wakes=2)
            shadow_end = start + timedelta(seconds=3)
            live_start = start + timedelta(seconds=6)
            result = repeatability.evaluate(
                shadow_state=shadow, live_state=live, start=start, shadow_end=shadow_end,
                live_start=live_start, required_days=30, required_wakes=4,
                credentials=Path("c"), cli=Path("a"))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["observed"]["shadow_wakes"], 2)
        self.assertEqual(result["observed"]["live_wakes"], 2)
        self.assertEqual(result["observed"]["unreported_wakes"], 0)
        self.assertEqual(result["transition"], {
            "shadow_end": shadow_end.isoformat(), "live_start": live_start.isoformat()})

    @patch.object(repeatability, "_official_orders")
    def test_incomplete_or_overlapping_transition_is_rejected(self, official):
        with tempfile.TemporaryDirectory() as directory:
            shadow, live, start = self.fixture(Path(directory), 1, 1)
            cases = [
                {"shadow_end": start + timedelta(seconds=1)},
                {"live_start": start + timedelta(seconds=2)},
                {"shadow_end": start + timedelta(seconds=2),
                 "live_start": start + timedelta(seconds=1)},
            ]
            for transition in cases:
                with self.subTest(transition=transition), self.assertRaisesRegex(
                        ValueError, "^repeatability_transition_invalid$"):
                    repeatability.evaluate(
                        shadow_state=shadow, live_state=live, start=start,
                        required_days=30, required_wakes=100,
                        credentials=Path("c"), cli=Path("a"), **transition)
        official.assert_not_called()

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
