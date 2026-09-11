#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import tempfile
import unittest

import run_contract
import run_with_contract
import verify_run_reports


class VerifyRunReportsTests(unittest.TestCase):
    def test_verifies_one_evidenced_delivery_for_every_current_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state = root / "state"
            evidence = root / "evidence"
            store = run_contract.RunStore(
                state / "run-reports.jsonl", state / "run-deliveries.jsonl")
            expected = {}
            for index, runner_id in enumerate(sorted(run_contract.RUNNERS), 1):
                result = run_with_contract.execute(
                    runner_id=runner_id,
                    command=["/usr/bin/true"],
                    state_root=state,
                    evidence_root=evidence,
                    environment="production",
                    dry_run=False,
                    product_ids=[],
                    quarantine_reason="fixture_quarantine",
                    send=False,
                )
                expected[runner_id] = result.event["run_id"]
                store.record_delivery(runner_id, result.event["run_id"], {
                    "status": "delivered", "chat_id": 42, "message_ids": [index]})
            verdict = verify_run_reports.verify(state, expected)
            self.assertTrue(verdict["passed"])
            self.assertEqual(verdict["runners_verified"], len(run_contract.RUNNERS))
            self.assertEqual(verdict["duplicate_final_keys"], [])
            self.assertEqual(verdict["duplicate_delivery_keys"], [])
            self.assertEqual(verdict["would_resend"], [])


if __name__ == "__main__":
    unittest.main()
