import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search_loop.mercor_human_gate import HumanGateStore
from job_search_loop.mercor_reporting import build_pass_message, report_pass, terminal_result


class MercorReportingTests(unittest.TestCase):
    def test_unacknowledged_outbox_delivery_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result.json"
            result.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
            with patch(
                "job_search_loop.mercor_reporting.send_once",
                return_value={"status": "send_started", "message_id": None},
            ):
                receipt = report_pass(
                    run_id="mercor-test-unacknowledged",
                    result_path=result,
                    outbox=root / "outbox.sqlite3",
                )
            with patch(
                "job_search_loop.mercor_reporting.send_once",
                side_effect=RuntimeError("offline"),
            ):
                exception_receipt = report_pass(
                    run_id="mercor-test-exception",
                    result_path=result,
                    outbox=root / "outbox.sqlite3",
                )

        self.assertEqual(receipt["delivery"], "delivery_unknown")
        self.assertEqual(receipt["event_key"], "mercor-pass:mercor-test-unacknowledged")
        self.assertEqual(exception_receipt["event_key"], "mercor-pass:mercor-test-exception")

    def test_terminal_failure_overrides_successful_provider_result(self):
        with tempfile.TemporaryDirectory() as directory:
            result = Path(directory) / "result.json"
            result.write_text(
                json.dumps({"status": "submitted", "submitted": [{"title": "Role"}]}),
                encoding="utf-8",
            )
            receipt = terminal_result(
                result_path=result,
                reason="earnings_capture_failed",
            )

        self.assertEqual(receipt["status"], "failed")
        self.assertIn("earnings_capture_failed", receipt["blocked"])

    def test_receipt_lists_one_id_for_one_reusable_human_ceremony(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result.json"
            result.write_text(
                json.dumps(
                    {
                        "status": "needs_human",
                        "needs_human": [
                            "Corporate Development: Finance Interview not done.",
                            "Corporate Treasury: Finance Interview not done.",
                        ],
                        "evidence": {"page_url": "https://work.mercor.com/explore"},
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "job_search_loop.mercor_reporting.send_once",
                return_value={"status": "sent", "message_id": "telegram-1"},
            ):
                receipt = report_pass(
                    run_id="mercor-test-reusable-gate",
                    result_path=result,
                    outbox=root / "outbox.sqlite3",
                    gate_store=root / "human-gates.jsonl",
                )

        self.assertEqual(len(receipt["human_gate_ids"]), 1)

    def test_grounded_pass_resolves_stale_auth_and_resume_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gates = root / "human-gates.jsonl"
            store = HumanGateStore(gates)
            store.record(
                run_id="old",
                reason="Mercor authentication is required; all pages showed Sign in.",
                evidence_ref="run:old",
            )
            store.record(
                run_id="old",
                reason="The supplied resume artifact was not present at the parent-provided path.",
                evidence_ref="run:old",
            )
            result = root / "result.json"
            result.write_text(
                json.dumps(
                    {
                        "status": "observed_no_action",
                        "inspected_listings": [{"listing_id": "list_1", "title": "Role"}],
                        "submitted": [],
                        "needs_human": [],
                        "blocked": [],
                        "evidence": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch("job_search_loop.mercor_reporting.send_once", return_value={"status": "sent", "message_id": "1"}):
                report_pass(
                    run_id="current",
                    result_path=result,
                    outbox=root / "outbox.sqlite3",
                    gate_store=gates,
                )
            self.assertEqual(store.pending(), [])
            self.assertEqual(len(gates.read_text(encoding="utf-8").splitlines()), 4)

    def test_message_is_compact_grounded_and_redacts_private_details(self):
        message = build_pass_message(
            run_id="mercor-test-1",
            result={
                "status": "observed_no_action",
                "inspected_listings": [
                    {
                        "title": "Data quality Evaluator",
                        "decision": "not_submitted_missing_fact",
                    }
                ],
                "submitted": [],
                "needs_human": [],
                "blocked": [],
            },
        )

        self.assertIn("Codex::: Mercor pass mercor-test-1", message)
        self.assertIn("observed_no_action", message)
        self.assertIn("Data quality Evaluator", message)
        self.assertNotIn("profile.json", message)
        self.assertNotIn("resume.pdf", message)

    def test_message_reports_submit_and_human_routes(self):
        message = build_pass_message(
            run_id="mercor-test-2",
            result={
                "status": "submitted",
                "inspected_listings": [],
                "submitted": [{"title": "Japanese Evaluator"}],
                "needs_human": ["interview_required"],
                "blocked": [],
            },
        )

        self.assertIn("submitted=Japanese Evaluator", message)
        self.assertIn("needs_human=interview_required", message)

    def test_report_references_canonical_gate_without_repeating_model_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result.json"
            result.write_text(json.dumps({
                "status": "needs_human",
                "inspected_listings": [],
                "submitted": [],
                "needs_human": ["provide Mac screenshot and interview"],
                "blocked": [],
                "evidence": {},
            }), encoding="utf-8")
            with patch("job_search_loop.mercor_reporting.send_once", return_value={"status": "sent", "message_id": "1"}):
                receipt = report_pass(
                    run_id="run-1",
                    result_path=result,
                    outbox=root / "outbox.sqlite3",
                    gate_store=root / "human-gates.jsonl",
                )
        self.assertNotIn("provide Mac screenshot", receipt["message"])
        self.assertIn("human_gate_ids=", receipt["message"])


if __name__ == "__main__":
    unittest.main()
