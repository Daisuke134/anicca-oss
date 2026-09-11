#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import tempfile
import unittest

import run_with_contract


class RunWithContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_captures_exact_process_evidence(self):
        result = run_with_contract.execute(
            runner_id="mine",
            command=["/bin/sh", "-c", "printf 'observed output\\n'"],
            state_root=self.root / "state",
            evidence_root=self.root / "evidence",
            environment="production",
            dry_run=False,
            product_ids=[],
            quarantine_reason=None,
            send=False,
        )
        self.assertEqual(result.event["status"], "success")
        self.assertEqual(result.event["metrics"][0]["name"], "process_exit_code")
        self.assertEqual(result.event["metrics"][0]["value"], 0)
        self.assertEqual(result.returncode, 0)
        stdout = pathlib.Path(result.event["evidence"][0]["path"])
        self.assertEqual(stdout.read_text(), "observed output\n")

    def test_failure_is_failed_with_nonzero_exit_metric(self):
        result = run_with_contract.execute(
            runner_id="score",
            command=["/bin/sh", "-c", "echo broken >&2; exit 7"],
            state_root=self.root / "state",
            evidence_root=self.root / "evidence",
            environment="production",
            dry_run=False,
            product_ids=[],
            quarantine_reason=None,
            send=False,
        )
        self.assertEqual(result.event["status"], "failed")
        self.assertEqual(result.event["metrics"][0]["value"], 7)
        self.assertIn("exit 7", result.event["error"])

    def test_quarantine_never_executes_command(self):
        marker = self.root / "must-not-exist"
        result = run_with_contract.execute(
            runner_id="clip",
            command=["/usr/bin/touch", str(marker)],
            state_root=self.root / "state",
            evidence_root=self.root / "evidence",
            environment="production",
            dry_run=False,
            product_ids=["ebook-en"],
            quarantine_reason="publisher_quarantined_until_gate_12",
            send=False,
        )
        self.assertFalse(marker.exists())
        self.assertEqual(result.event["status"], "skipped")
        self.assertEqual(result.event["metrics"][0]["name"], "external_actions")
        self.assertEqual(result.event["metrics"][0]["value"], 0)

    def test_dry_run_is_always_test_and_simulated(self):
        result = run_with_contract.execute(
            runner_id="capafy",
            command=["/usr/bin/true"],
            state_root=self.root / "state",
            evidence_root=self.root / "evidence",
            environment="production",
            dry_run=True,
            product_ids=["ebook-en"],
            quarantine_reason=None,
            send=False,
        )
        self.assertEqual(result.event["environment"], "test")
        self.assertTrue(result.event["metrics"][0]["simulated"])
        self.assertTrue(result.event["dry_run"])


if __name__ == "__main__":
    unittest.main()
