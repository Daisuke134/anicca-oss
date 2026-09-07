import tempfile
import unittest
from pathlib import Path

from job_search_loop.mercor_provider import MercorListing
from job_search_loop.mercor_submit_guard import (
    MercorSubmitGuardError,
    claim_ready_submission,
    claim_submission_once,
    classify_submit_readback,
    fenced_listing_ids,
)


class MercorSubmitGuardTests(unittest.TestCase):
    def _listing(self) -> MercorListing:
        return MercorListing(
            listing_id="list-new",
            title="Software / AI / IT / data Evaluator",
            url="https://work.mercor.com/explore?listingId=list-new",
            application_state="ready_to_submit",
            steps_completed=3,
            submit_visible=True,
            domain_expert_reused=True,
        )

    def test_claim_requires_ready_state_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "pre.json"
            evidence.write_text('{"observed":true}\n', encoding="utf-8")
            claim = claim_ready_submission(
                self._listing(), submitted_listing_ids=set(), pre_submit_evidence=evidence
            )
            self.assertIsNotNone(claim)
            self.assertEqual(len(claim.claim_token), 64)

    def test_duplicate_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "pre.json"
            evidence.write_text('{"observed":true}\n', encoding="utf-8")
            self.assertIsNone(
                claim_ready_submission(
                    self._listing(), submitted_listing_ids={"list-new"}, pre_submit_evidence=evidence
                )
            )

    def test_missing_evidence_fails_closed(self):
        with self.assertRaises(MercorSubmitGuardError):
            claim_ready_submission(
                self._listing(),
                submitted_listing_ids=set(),
                pre_submit_evidence=Path("/tmp/no-such-mercor-evidence.json"),
            )

    def test_readback_is_authoritative_or_unknown(self):
        self.assertEqual(
            classify_submit_readback(
                page_url="https://work.mercor.com/jobs/apply/candidate-x",
                visible_text="Your application has been submitted!",
            ),
            "submitted_pending_review",
        )
        self.assertEqual(
            classify_submit_readback(
                page_url="https://work.mercor.com/jobs/apply/candidate-x",
                visible_text="Loading...",
            ),
            "submit_unknown",
        )

    def test_ready_state_supports_four_step_roles(self):
        listing = MercorListing(
            listing_id="list-four", title="Japanese Systems Expert",
            url="https://work.mercor.com/explore?listingId=list-four",
            application_state="ready_to_submit", steps_completed=4,
            submit_visible=True, domain_expert_reused=True, steps_total=4,
        )
        self.assertIsNotNone(claim_ready_submission(
            listing, submitted_listing_ids=set(),
            pre_submit_evidence=self._temporary_evidence(),
        ))

    def _temporary_evidence(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, directory)
        path = Path(directory) / "pre.json"
        path.write_text('{"observed":true}\n', encoding="utf-8")
        return path

    def test_persistent_claim_survives_crash_and_duplicate_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "pre.json"
            evidence.write_text('{"observed":true}\n', encoding="utf-8")
            fences = root / "submission-fences.jsonl"

            first = claim_submission_once(
                fence_ledger=fences,
                listing_id="list-new",
                title="Software Evaluator",
                url="https://work.mercor.com/jobs/list-new/software-evaluator",
                pre_submit_evidence=evidence,
                run_id="run-1",
            )
            replay = claim_submission_once(
                fence_ledger=fences,
                listing_id="list-new",
                title="Software Evaluator",
                url="https://work.mercor.com/jobs/list-new/software-evaluator",
                pre_submit_evidence=evidence,
                run_id="run-2",
            )

            self.assertTrue(first)
            self.assertFalse(replay)
            self.assertEqual(fenced_listing_ids(fences), {"list-new"})
            self.assertEqual(len(fences.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
