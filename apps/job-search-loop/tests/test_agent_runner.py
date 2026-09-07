import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from job_search_loop.agent_runner import (
    AgentRunner,
    ContractError,
    PassAlreadyRunning,
    TASK_CLASSES,
)


class AgentRunnerTests(unittest.TestCase):
    def test_default_runner_is_the_canonical_life_manager_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = AgentRunner(evidence_root=Path(directory) / "evidence")
            repo_root = Path(__file__).resolve().parents[3]
            self.assertEqual(
                runner.runner_path,
                repo_root / "runtime" / "agent-runner" / "agent_runner.py",
            )

    def test_task_routes_are_pinned(self):
        self.assertEqual(TASK_CLASSES["tailor"], "composition-agent")
        self.assertEqual(TASK_CLASSES["inbox"], "composition-agent")
        self.assertEqual(TASK_CLASSES["submit"], "browser-lane-agent")
        self.assertEqual(TASK_CLASSES["improve"], "high-value-agent")

    def test_mercor_pass_supplies_required_escalation_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = AgentRunner(
                runner_path=Path("/opt/agent_runner.py"),
                evidence_root=root / "evidence",
            )
            completed = type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "status": "success",
                            "result_path": str(root / "result.json"),
                        }
                    ),
                    "stderr": "",
                },
            )()
            (root / "result.json").write_text('{"answer":"ok"}', encoding="utf-8")
            schema = root / "schema.json"
            schema.write_text('{"type":"object","required":["answer"]}', encoding="utf-8")
            with patch("subprocess.run", return_value=completed) as call:
                runner.run(
                    task="mercor_pass",
                    prompt="Grounded task",
                    schema_path=schema,
                    workdir=root,
                    run_id="mercor-pass",
                )
            argv = call.call_args.args[0]
            self.assertIn("--escalation-reason", argv)
            self.assertIn("Mercor application", argv[argv.index("--escalation-reason") + 1])

    def test_composition_prompt_uses_stdin_and_retains_private_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = AgentRunner(
                runner_path=Path("/opt/agent_runner.py"),
                evidence_root=root / "evidence",
            )
            completed = type(
                "Completed",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "status": "success",
                            "result_path": str(root / "result.json"),
                        }
                    ),
                    "stderr": "",
                },
            )()
            (root / "result.json").write_text('{"answer":"ok"}', encoding="utf-8")
            schema = root / "schema.json"
            schema.write_text(
                '{"type":"object","required":["answer"]}', encoding="utf-8"
            )
            with patch("subprocess.run", return_value=completed) as call:
                result = runner.run(
                    task="tailor",
                    prompt="Grounded task",
                    schema_path=schema,
                    workdir=root,
                    run_id="one",
                )
            argv = call.call_args.args[0]
            self.assertIn("--prompt-stdin", argv)
            self.assertNotIn("--prompt-file", argv)
            self.assertEqual(call.call_args.kwargs["input"], "Grounded task")
            self.assertNotIn("Grounded task", argv)
            prompt_path = root / "evidence" / "one" / "prompt.md"
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), "Grounded task")
            self.assertEqual(prompt_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(result["answer"], "ok")

    def test_missing_required_result_field_fails_closed(self):
        with self.assertRaises(ContractError):
            AgentRunner.validate({"other": True}, {"required": ["answer"]})

    def test_identified_busy_runner_is_not_a_contract_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = AgentRunner(
                runner_path=Path("/opt/agent_runner.py"),
                evidence_root=root / "evidence",
            )
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            completed = type("Completed", (), {
                "returncode": 75,
                "stdout": "",
                "stderr": "LIFE_MANAGER_PROVIDER_LEASE_BUSY\nprovider lease busy\n",
            })()
            with patch("subprocess.run", return_value=completed):
                with self.assertRaises(PassAlreadyRunning):
                    runner.run(
                        task="mercor_pass", prompt="Grounded task",
                        schema_path=schema, workdir=root, run_id="busy",
                    )

    def test_unidentified_rc75_remains_a_contract_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = AgentRunner(
                runner_path=Path("/opt/agent_runner.py"),
                evidence_root=root / "evidence",
            )
            schema = root / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            completed = type("Completed", (), {
                "returncode": 75, "stdout": "", "stderr": "budget blocked\n",
            })()
            with patch("subprocess.run", return_value=completed):
                with self.assertRaises(ContractError):
                    runner.run(
                        task="mercor_pass", prompt="Grounded task",
                        schema_path=schema, workdir=root, run_id="budget-blocked",
                    )


if __name__ == "__main__":
    unittest.main()
