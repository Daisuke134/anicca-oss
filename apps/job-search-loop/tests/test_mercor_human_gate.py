import tempfile
import unittest
from pathlib import Path

from job_search_loop.mercor_human_gate import HumanGateStore


class MercorHumanGateTests(unittest.TestCase):
    def test_gate_is_idempotent_and_pending_is_replayable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HumanGateStore(Path(directory) / "human-gates.jsonl")
            first = store.record(
                run_id="run-1",
                reason="assessment_required",
                evidence_ref="evidence://assessment",
            )
            duplicate = store.record(
                run_id="run-1",
                reason="assessment_required",
                evidence_ref="evidence://assessment",
            )
            self.assertEqual(first, duplicate)
            self.assertEqual(len(store.pending()), 1)
            self.assertEqual(store.pending()[0]["reason"], "assessment_required")
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_different_reason_creates_distinct_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HumanGateStore(Path(directory) / "human-gates.jsonl")
            first = store.record(run_id="run-1", reason="assessment_required", evidence_ref="evidence://a")
            second = store.record(run_id="run-1", reason="missing_fact", evidence_ref="evidence://b")
            self.assertNotEqual(first["gate_id"], second["gate_id"])
            self.assertEqual(len(store.pending()), 2)

    def test_project_thor_wording_drift_does_not_duplicate_pending_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HumanGateStore(Path(directory) / "human-gates.jsonl")
            first = store.record(
                run_id="run-1",
                reason="list_abc: Project Thor Assessment is the next required step; it was not started.",
                evidence_ref="https://work.mercor.com/explore?listingId=list_abc",
            )
            second = store.record(
                run_id="run-2",
                reason="Project Thor Assessment required for Generalist (Macbook User)",
                evidence_ref="https://work.mercor.com/explore?listingId=list_other",
            )
            self.assertEqual(first["gate_id"], second["gate_id"])
            self.assertEqual(len(store.pending()), 1)

    def test_finance_interview_wording_drift_does_not_duplicate_pending_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HumanGateStore(Path(directory) / "human-gates.jsonl")
            first = store.record(
                run_id="run-1",
                reason="Corporate Development Expert: Finance Interview is the next required step.",
                evidence_ref="https://work.mercor.com/explore?listingId=list_a",
            )
            second = store.record(
                run_id="run-2",
                reason="Finance Interview required for Corporate Development Expert",
                evidence_ref="https://work.mercor.com/explore?listingId=list_b",
            )
            self.assertEqual(first["gate_id"], second["gate_id"])
            self.assertEqual(len(store.pending()), 1)

    def test_bilingual_competency_collapses_legacy_listing_specific_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HumanGateStore(Path(directory) / "human-gates.jsonl")
            first = store.record(
                run_id="run-1",
                reason="list-japanese: Complete the required Bilingual Competency AI interview",
                evidence_ref="run:run-1/japanese",
            )
            second = store.record(
                run_id="run-2",
                reason="list-pdf: Bilingual Competency remains not done; camera is required",
                evidence_ref="run:run-2/pdf",
            )
            self.assertEqual(first["gate_id"], second["gate_id"])
            self.assertEqual(len(store.pending()), 1)

    def test_named_ceremony_wording_and_run_evidence_drift_collapses(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HumanGateStore(Path(directory) / "human-gates.jsonl")
            first = store.record(
                run_id="run-1",
                reason="list_bio: Pharmacology Lab Review assessment not done.",
                evidence_ref="run:run-1",
            )
            second = store.record(
                run_id="run-2",
                reason="Biology Research Scientist: Pharmacology Lab Review is not done; human action required",
                evidence_ref="run:run-2",
            )
            survey = store.record(
                run_id="run-2",
                reason="Healthcare Administrative Specialist: Professional Work Survey is not done.",
                evidence_ref="run:run-2",
            )
            self.assertEqual(first["gate_id"], second["gate_id"])
            self.assertNotEqual(first["gate_id"], survey["gate_id"])
            self.assertEqual(len(store.pending()), 2)

    def test_resolution_is_append_only_and_removes_current_pending_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "human-gates.jsonl"
            store = HumanGateStore(path)
            auth = store.record(
                run_id="run-1",
                reason="Mercor authentication is required; all pages showed Sign in.",
                evidence_ref="run:run-1",
            )
            store.record(
                run_id="run-1",
                reason="The supplied resume artifact was not present at the parent-provided path.",
                evidence_ref="run:run-1",
            )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
            resolved = store.resolve(
                identity_key="mercor_authentication",
                run_id="run-2",
                evidence_ref="run:run-2",
            )
            self.assertEqual(resolved["gate_id"], auth["gate_id"])
            self.assertEqual(resolved["status"], "resolved")
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 3)
            self.assertEqual(len(store.pending()), 1)

    def test_known_listing_and_title_map_legacy_wording_to_current_ceremony(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HumanGateStore(Path(directory) / "human-gates.jsonl")
            named = store.record(
                run_id="run-1",
                reason="Project Thor Assessment required.",
                evidence_ref="run:run-1",
            )
            legacy = store.record(
                run_id="run-2",
                reason="Generalist (Macbook User): assessment not done; do not start it.",
                evidence_ref="run:run-2",
            )
            survey = store.record(
                run_id="run-2",
                reason="Mercor UI showed a required assessment; it was not started.",
                evidence_ref="https://work.mercor.com/explore?listingId=list_AAABn3FrsqJqPFplFuhEEYix",
            )
            self.assertEqual(named["gate_id"], legacy["gate_id"])
            self.assertNotEqual(named["gate_id"], survey["gate_id"])
            self.assertEqual(survey["identity_key"], "professional_work_survey")
            self.assertEqual(len(store.pending()), 2)

if __name__ == "__main__":
    unittest.main()
