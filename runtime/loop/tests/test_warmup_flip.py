import datetime
import importlib.util
import json
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "warmup_flip", ROOT / "skills/anicca-warmup-flip/scripts/warmup_flip.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WarmupFlipTest(unittest.TestCase):
    def _fixture(self, root: Path, sender_exit: int = 0):
        state = root / "state/postiz-integrations.json"
        state.parent.mkdir(parents=True)
        state.write_text(json.dumps({"integrations": [
            {"handle": "ready", "warmup_phase": "warmup", "warmup_started_at": "2026-09-01"},
            {"handle": "new", "warmup_phase": "warmup"},
        ]}))
        sender = root / "sender.sh"
        sender.write_text(f"#!/bin/sh\nexit {sender_exit}\n")
        return state, sender

    def test_flips_atomically_and_delivers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, sender = self._fixture(root)
            self.assertEqual(MODULE.run(state, sender, datetime.date(2026, 9, 10)), 0)
            body = json.loads(state.read_text())
            self.assertEqual(body["integrations"][0]["warmup_phase"], "live")
            self.assertEqual(body["integrations"][1]["warmup_started_at"], "2026-09-10")
            self.assertFalse((root / "pending-telegram.txt").exists())
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_failed_delivery_is_retried_next_wake(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, sender = self._fixture(root, sender_exit=7)
            self.assertEqual(MODULE.run(state, sender, datetime.date(2026, 9, 10)), 7)
            pending = root / "pending-telegram.txt"
            self.assertTrue(pending.exists())
            sender.write_text("#!/bin/sh\nexit 0\n")
            self.assertEqual(MODULE.run(state, sender, datetime.date(2026, 9, 10)), 0)
            self.assertFalse(pending.exists())

    def test_concurrent_wakes_leave_valid_complete_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, sender = self._fixture(root)
            results = []
            threads = [threading.Thread(
                target=lambda: results.append(MODULE.run(state, sender, datetime.date(2026, 9, 10)))
            ) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(results, [0, 0, 0, 0])
            body = json.loads(state.read_text())
            self.assertEqual(body["integrations"][0]["warmup_phase"], "live")
            self.assertEqual(body["integrations"][1]["warmup_started_at"], "2026-09-10")

    def test_transaction_recovers_after_state_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, sender = self._fixture(root)
            original = MODULE._atomic_text
            failed = False

            def fail_state_once(path, body):
                nonlocal failed
                if path == state and not failed:
                    failed = True
                    raise OSError("crash before state commit")
                return original(path, body)

            with mock.patch.object(MODULE, "_atomic_text", side_effect=fail_state_once):
                with self.assertRaisesRegex(OSError, "crash"):
                    MODULE.run(state, sender, datetime.date(2026, 9, 10))
            self.assertTrue((root / "warmup-flip-transaction.json").exists())
            self.assertEqual(MODULE.run(state, sender, datetime.date(2026, 9, 10)), 0)
            self.assertFalse((root / "warmup-flip-transaction.json").exists())
            self.assertEqual(json.loads(state.read_text())["integrations"][0]["warmup_phase"], "live")

    def test_rejects_non_object_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, sender = self._fixture(root)
            state.write_text('{"integrations":[1]}')
            with self.assertRaisesRegex(ValueError, "invalid"):
                MODULE.run(state, sender, datetime.date(2026, 9, 10))


if __name__ == "__main__":
    unittest.main()
