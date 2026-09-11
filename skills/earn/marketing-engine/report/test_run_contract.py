#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

import run_contract


def valid_event(**overrides):
    event = {
        "schema_version": "marketing.run.v1",
        "run_id": "0123456789abcdef0123456789abcdef",
        "runner_id": "metrics",
        "environment": "production",
        "started_at": "2026-08-01T00:00:00Z",
        "finished_at": "2026-08-01T00:00:01Z",
        "status": "success",
        "dry_run": False,
        "product_ids": ["anicca-ios"],
        "effects": [{
            "provider": "revenuecat",
            "action": "read_chart",
            "status": "observed",
            "receipt": "chart:mrr:app511ef26659:2026-07-30",
            "evidence": "evidence/business/gate5.json",
            "null_reason": None,
            "simulated": False,
        }],
        "metrics": [{
            "name": "mrr",
            "product_id": "anicca-ios",
            "value": 20.73,
            "unit": "USD",
            "observed_at": "2026-07-30T23:59:59Z",
            "source": "revenuecat",
            "evidence": "evidence/business/gate5.json",
            "null_reason": None,
            "simulated": False,
        }],
        "evidence": [{
            "path": "evidence/runs/metrics/stdout.txt",
            "sha256": "a" * 64,
            "bytes": 12,
            "kind": "stdout",
        }],
        "error": None,
    }
    event.update(overrides)
    return event


class ContractTests(unittest.TestCase):
    def test_machine_schema_is_valid_json_and_names_all_runners(self):
        schema = json.loads((pathlib.Path(__file__).parent / "run-contract.schema.json").read_text())
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(set(schema["properties"]["runner_id"]["enum"]), set(run_contract.RUNNERS))

    def test_valid_event_passes(self):
        self.assertEqual(run_contract.validate_event(valid_event())["runner_id"], "metrics")

    def test_run_id_is_nonzero_32_lower_hex(self):
        for bad in ("0" * 32, "ABCDEF0123456789ABCDEF0123456789", "abc", "g" * 32):
            with self.subTest(bad=bad), self.assertRaises(run_contract.ContractError):
                run_contract.validate_event(valid_event(run_id=bad))

    def test_dry_run_cannot_be_production(self):
        with self.assertRaisesRegex(run_contract.ContractError, "dry_run"):
            run_contract.validate_event(valid_event(dry_run=True))

    def test_production_cannot_contain_simulated_data(self):
        event = valid_event()
        event["metrics"][0]["simulated"] = True
        with self.assertRaisesRegex(run_contract.ContractError, "simulated"):
            run_contract.validate_event(event)

    def test_null_metric_requires_reason_and_zero_requires_evidence(self):
        event = valid_event()
        event["metrics"][0].update(value=None, null_reason=None)
        with self.assertRaisesRegex(run_contract.ContractError, "null_reason"):
            run_contract.validate_event(event)
        event = valid_event()
        event["metrics"][0].update(value=0, evidence=None)
        with self.assertRaisesRegex(run_contract.ContractError, "evidence"):
            run_contract.validate_event(event)

    def test_effect_requires_receipt_or_null_reason(self):
        event = valid_event()
        event["effects"][0].update(receipt=None, null_reason=None)
        with self.assertRaisesRegex(run_contract.ContractError, "receipt"):
            run_contract.validate_event(event)

    def test_every_report_has_a_metric_even_when_unavailable(self):
        with self.assertRaisesRegex(run_contract.ContractError, "metrics"):
            run_contract.validate_event(valid_event(metrics=[]))

    def test_all_eight_runner_ids_are_accepted(self):
        for runner_id in sorted(run_contract.RUNNERS):
            with self.subTest(runner_id=runner_id):
                self.assertEqual(
                    run_contract.validate_event(valid_event(runner_id=runner_id))["runner_id"],
                    runner_id,
                )


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.store = run_contract.RunStore(root / "runs.jsonl", root / "deliveries.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_equivalent_replay_is_deduplicated(self):
        first = self.store.record_final(valid_event())
        second = self.store.record_final(valid_event())
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(len(self.store.final_events()), 1)

    def test_conflicting_replay_fails_closed(self):
        self.store.record_final(valid_event())
        changed = valid_event(status="partial")
        with self.assertRaises(run_contract.ConflictError):
            self.store.record_final(changed)

    def test_telegram_delivery_is_sent_once(self):
        calls = []

        def send(text):
            calls.append(text)
            return {"status": "delivered", "message_ids": [777], "chat_id": 42}

        first = run_contract.record_and_deliver(valid_event(), self.store, send)
        second = run_contract.record_and_deliver(valid_event(), self.store, send)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["message_ids"], [777])
        self.assertEqual(second["message_ids"], [777])
        self.assertEqual(len(self.store.deliveries()), 1)

    def test_render_only_contains_validated_facts(self):
        text = run_contract.render_telegram(valid_event())
        self.assertIn("metrics · success · production", text)
        self.assertIn("mrr=20.73 USD", text)
        self.assertIn("revenuecat", text)
        self.assertNotIn("estimated", text.lower())

    def test_capture_artifact_hashes_exact_bytes(self):
        path = pathlib.Path(self.tmp.name) / "stdout.txt"
        path.write_bytes(b"truth\n")
        item = run_contract.evidence_item(path, "stdout")
        self.assertEqual(item["bytes"], 6)
        self.assertEqual(item["sha256"], "5eae1ab667f5669d71b87b302102134aa1ac4835b449e66abfbe723129e10d3a")


if __name__ == "__main__":
    unittest.main()
