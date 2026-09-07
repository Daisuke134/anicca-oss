import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from job_search_loop.agent_runner import AgentRunner, PassAlreadyRunning, TASK_CLASSES
from job_search_loop.mercor_pass import (
    build_context,
    main,
    record_inspections,
    record_verified_submissions,
    validate_bounded_scan,
    validate_evidence_paths,
)


ROOT = Path(__file__).resolve().parents[1]


class MercorPassContractTests(unittest.TestCase):
    def test_inspections_become_a_durable_next_wake_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            record_inspections(state, {
                "inspected_listings": [{"listing_id": "list-seen", "decision": "not_fit"}]
            }, run_id="run-1")
            context = build_context(
                state_root=state,
                profile_path=state / "profile.json",
                resume_path=state / "resume.pdf",
                cdp_url="http://127.0.0.1:9222",
            )
            self.assertEqual(context["recently_inspected_listing_ids"], ["list-seen"])

    def test_mercor_is_retired_locally_but_keeps_portable_thirty_minute_cadence(self):
        registry = json.loads((ROOT.parents[1] / "config" / "loop-registry.json").read_text())
        self.assertNotIn("job-search-mercor", registry["loops"])
        self.assertIn("ai.anicca.job-search-mercor", registry["retired_labels"])
        provider_registry = (ROOT.parents[1] / "loops" / "job-hunter" / "registry.yaml").read_text()
        mercor = provider_registry.split("  - id: mercor\n", 1)[1]
        self.assertIn("interval_seconds: 1800", mercor)

    def _run_shell_with_pass_rc(self, *, pass_rc: int, pass_stderr: str):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        calls = root / "reporting-call.json"
        fake_python = root / "python"
        fake_python.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "argv = sys.argv[1:]\n"
            "if argv[:2] == ['-m', 'job_search_loop.browser_owner']:\n"
            "    pathlib.Path(argv[argv.index('--output') + 1]).write_text('{}\\n')\n"
            "    raise SystemExit(0)\n"
            "if argv[:2] == ['-m', 'job_search_loop.mercor_pass']:\n"
            "    print(os.environ['MERCOR_TEST_PASS_STDERR'], file=sys.stderr, end='')\n"
            "    raise SystemExit(int(os.environ['MERCOR_TEST_PASS_RC']))\n"
            "if argv[:2] == ['-m', 'job_search_loop.mercor_reporting']:\n"
            "    pathlib.Path(os.environ['MERCOR_TEST_REPORTING_CALL']).write_text(json.dumps(argv))\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)
        result = subprocess.run(
            ["/bin/zsh", str(ROOT / "scripts" / "run-mercor.sh")],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "HOME": str(root / "home"),
                "XDG_DATA_HOME": str(root / "data"),
                "JOB_SEARCH_STATE_ROOT": str(root / "state"),
                "JOB_SEARCH_PYTHON": str(fake_python),
                "MERCOR_TEST_PASS_RC": str(pass_rc),
                "MERCOR_TEST_PASS_STDERR": pass_stderr,
                "MERCOR_TEST_REPORTING_CALL": str(calls),
            },
        )
        reporting_call = json.loads(calls.read_text(encoding="utf-8"))
        return result, reporting_call

    def test_task_class_is_modelled_browser_lane(self):
        self.assertEqual(TASK_CLASSES["mercor_pass"], "browser-lane-agent")

    def test_prompt_contains_model_led_submit_guard_and_human_stop(self):
        prompt = (ROOT / "prompts" / "mercor-pass.md").read_text(encoding="utf-8")
        for required in (
            "model-led",
            "`N of N` and `100%`",
            "Submit application",
            "continue to the next distinct listing",
            "not a terminal pass result",
            "never retry",
            "needs_human",
            "browser Google 2FA button named `はい`",
            "Never write evidence",
            "only the current `evidence_dir`",
            "exact `evidence_dir` supplied",
            "never inspect or reuse an older `model-pass-*` directory",
            "visible pagination controls",
            "button titled `Page N` or `Next`",
            "up to four additional pages",
            "with a bounded maximum of twelve candidate",
            "detail pages per wake",
            "Never stop after the first Explore page",
            "Submit every ready distinct listing",
            "continue to the next distinct listing after each verified submission",
            "current-pass submitted set",
            "python3 -m job_search_loop.mercor_submit_guard",
            '"claimed": true',
            '"claimed": false',
            "capability_catalog_path",
            "Japan-eligible Japanese-language",
            "mercor_human_gate_notify",
        ):
            self.assertIn(required, prompt)
        self.assertNotIn("Choose at most one new listing", prompt)

    def test_result_contract_allows_every_bounded_candidate_to_be_submitted(self):
        schema = json.loads(
            (ROOT / "schemas" / "mercor-pass-result.v1.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(schema["properties"]["submitted"]["maxItems"], 12)

    def test_nonblocked_pass_cannot_quit_after_two_of_twelve_visible_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dom = root / "page.html"
            dom.write_text("\n".join(
                f'<a href="/explore?listingId=list_{index}">Role</a>'
                for index in range(12)
            ), encoding="utf-8")
            result = {
                "status": "observed_no_action",
                "inspected_listings": [
                    {"listing_id": "list_0"}, {"listing_id": "list_1"}
                ],
                "evidence": {"dom_path": str(dom)},
            }
            with self.assertRaisesRegex(ValueError, "bounded_scan_incomplete:2_of_12"):
                validate_bounded_scan(result)

    def test_transient_blocker_may_end_a_partial_scan(self):
        validate_bounded_scan({
            "status": "blocked",
            "inspected_listings": [],
            "evidence": {"dom_path": "/not/read"},
        })

    def test_current_skill_and_spec_match_continuous_application_policy(self):
        skill = (ROOT.parents[1] / "skills" / "mercor" / "SKILL.md").read_text()
        self.assertIn("30-minute", skill)
        self.assertIn("every grounded ready listing", skill)
        self.assertNotIn("existing hourly Job Hunter loop", skill)
        self.assertNotIn("submit exactly one new listing", skill)
        spec = (
            ROOT.parents[1]
            / "docs"
            / "superpowers"
            / "specs"
            / "2026-08-22-mercor-life-manager-consolidation.md"
        ).read_text()
        self.assertIn("existing 30-minute Job Hunter acquisition loop", spec)
        self.assertIn("submit every grounded ready listing", spec)

    def test_success_result_contract_validates(self):
        schema = json.loads(
            (ROOT / "schemas" / "mercor-pass-result.v1.schema.json").read_text(encoding="utf-8")
        )
        result = {
            "status": "submitted",
            "inspected_listings": [{
                "listing_id": "list-test",
                "url": "https://work.mercor.com/jobs/test",
                "title": "Software Evaluator",
                "application_state": "3/3",
                "submit_visible": True,
                "decision": "submitted",
            }],
            "submitted": [{
                "listing_id": "list-test",
                "title": "Software Evaluator",
                "url": "https://work.mercor.com/jobs/test",
                "status": "submitted_pending_review",
                "evidence_url": "https://work.mercor.com/jobs/apply/test",
                "evidence_path": "/tmp/evidence.json",
            }],
            "needs_human": [],
            "blocked": [],
            "evidence": {
                "page_url": "https://work.mercor.com/jobs/apply/test",
                "screenshot_path": "/tmp/screenshot.png",
                "dom_path": "/tmp/dom.json",
            },
        }
        AgentRunner.validate(result, schema)

    def test_runner_snapshots_prompt_and_schema_into_private_pass_evidence(self):
        script = (ROOT / "scripts" / "run-mercor.sh").read_text(encoding="utf-8")
        for required in (
            'PASS_PROMPT="$EVIDENCE/mercor-pass.md"',
            'PASS_SCHEMA="$EVIDENCE/mercor-pass-result.v1.schema.json"',
            'cp "$JOB_SEARCH_APP_ROOT/schemas/mercor-pass-result.v1.schema.json" "$PASS_SCHEMA"',
            '--prompt "$PASS_PROMPT"',
            '--schema "$PASS_SCHEMA"',
        ):
            self.assertIn(required, script)

    def test_runner_normalizes_model_evidence_modes_after_the_pass(self):
        script = (ROOT / "scripts" / "run-mercor.sh").read_text(encoding="utf-8")
        self.assertIn('find "$EVIDENCE" -type d -exec chmod 700 {} +', script)
        self.assertIn('find "$EVIDENCE" -type f -exec chmod 600 {} +', script)

    def test_busy_runner_returns_75_without_a_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence" / "agent"
            with patch(
                "job_search_loop.mercor_pass.run_pass",
                side_effect=PassAlreadyRunning(),
            ):
                self.assertEqual(main([
                    "--state-root", str(root / "state"),
                    "--profile", str(root / "profile.json"),
                    "--resume", str(root / "resume.pdf"),
                    "--cdp-url", "http://127.0.0.1:9334",
                    "--prompt", str(root / "prompt.md"),
                    "--schema", str(root / "schema.json"),
                    "--evidence-dir", str(evidence),
                    "--workdir", str(root),
                    "--run-id", "busy",
                ]), 75)
            self.assertFalse((evidence / "mercor-pass-summary.json").exists())

    def test_shell_maps_only_identified_busy_to_already_running(self):
        busy, busy_call = self._run_shell_with_pass_rc(
            pass_rc=75,
            pass_stderr="LIFE_MANAGER_PROVIDER_LEASE_BUSY\n",
        )
        self.assertEqual(busy.returncode, 0, busy.stderr)
        self.assertEqual(
            busy_call[busy_call.index("--reason") + 1],
            "mercor_pass_already_running",
        )
        budget, budget_call = self._run_shell_with_pass_rc(
            pass_rc=75,
            pass_stderr="budget blocked\n",
        )
        self.assertEqual(budget.returncode, 75, budget.stderr)
        self.assertEqual(
            budget_call[budget_call.index("--reason") + 1],
            "mercor_runner_failed",
        )

    def test_evidence_paths_must_stay_inside_current_private_pass(self):
        with self.subTest("stale evidence path is rejected"):
            with self.assertRaises(ValueError):
                validate_evidence_paths(
                    {
                        "status": "needs_human",
                        "evidence": {
                            "page_url": "https://work.mercor.com/explore",
                            "screenshot_path": "/tmp/old-pass/screenshot.png",
                            "dom_path": "/tmp/old-pass/dom.json",
                        },
                        "submitted": [],
                    },
                    Path("/tmp/current-pass"),
                )

    def test_evidence_paths_accept_existing_files_inside_current_private_pass(self):
        with self.subTest("current evidence is accepted"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                screenshot = root / "screenshot.png"
                dom = root / "dom.json"
                screenshot.write_bytes(b"png")
                dom.write_text("{}", encoding="utf-8")
                validate_evidence_paths(
                    {
                        "status": "needs_human",
                        "evidence": {
                            "page_url": "https://work.mercor.com/explore",
                            "screenshot_path": str(screenshot),
                            "dom_path": str(dom),
                        },
                        "submitted": [],
                    },
                    root,
                )

    def test_context_binds_the_current_evidence_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            context = build_context(
                state_root=state,
                profile_path=root / "profile.json",
                resume_path=root / "resume.pdf",
                cdp_url="http://127.0.0.1:9334",
                evidence_dir=root / "evidence" / "current-pass",
            )
            self.assertEqual(
                context["evidence_dir"],
                str((root / "evidence" / "current-pass").resolve()),
            )

    def test_context_deduplicates_persistent_pre_effect_claim_after_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            (state / "submission-fences.jsonl").write_text(
                '{"listing_id":"list-claimed","status":"submit_claimed"}\n',
                encoding="utf-8",
            )
            context = build_context(
                state_root=state,
                profile_path=root / "profile.json",
                resume_path=root / "resume.pdf",
                cdp_url="http://127.0.0.1:9334",
            )
            self.assertIn("list-claimed", context["submitted_listing_ids"])
            self.assertEqual(
                context["submission_fence_ledger"],
                str((state / "submission-fences.jsonl").resolve()),
            )

    def test_verified_submissions_are_recorded_once_for_next_wake(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            result = {
                "status": "submitted",
                "submitted": [
                    {
                        "listing_id": "list-one",
                        "title": "Software Evaluator",
                        "url": "https://work.mercor.com/jobs/list-one/software-evaluator",
                        "status": "submitted_pending_review",
                        "evidence_url": "https://work.mercor.com/jobs/apply/candidate-one",
                        "evidence_path": "/tmp/evidence-one.json",
                    },
                    {
                        "listing_id": "list-two",
                        "title": "Data Evaluator",
                        "url": "https://work.mercor.com/jobs/list-two/data-evaluator",
                        "status": "submitted_pending_review",
                        "evidence_url": "https://work.mercor.com/jobs/apply/candidate-two",
                        "evidence_path": "/tmp/evidence-two.json",
                    },
                ],
            }
            record_verified_submissions(state, result, run_id="run-1")
            record_verified_submissions(state, result, run_id="run-replay")
            ledger = state / "applications.jsonl"
            rows = [json.loads(line) for line in ledger.read_text().splitlines()]
            self.assertEqual([row["listing_id"] for row in rows], ["list-one", "list-two"])


if __name__ == "__main__":
    unittest.main()
