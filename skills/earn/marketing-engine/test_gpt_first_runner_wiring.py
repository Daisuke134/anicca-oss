import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "earn" / "marketing-engine"
RUN_AGENT = ENGINE / "run_agent.sh"
CONSUMERS = (
    ROOT / "self" / "capafy-loop" / "capafy-loop-daily.sh",
    ROOT / "earn" / "capafy-marketing" / "capafy-ig-marketing-daily.sh",
    ENGINE / "spawn-marketing-loop.sh",
    ROOT / "self" / "self-fix.sh",
)

EXPECTED_TASK_CLASSES = {
    # capafy drives the Capafy CP1/CP2/CP3 publish UI; a measured read-only
    # pass is already 229s, so 180s (tool-agent) SIGKILLed it daily (X23).
    ROOT / "self" / "capafy-loop" / "capafy-loop-daily.sh": "browser-lane-agent",
    ROOT / "earn" / "capafy-marketing" / "capafy-ig-marketing-daily.sh": "marketing-agent",
    ENGINE / "spawn-marketing-loop.sh": "repeatable-agent",
    ROOT / "self" / "self-fix.sh": "high-value-agent",
}

EXPECTED_LOOPS = {
    ROOT / "self" / "capafy-loop" / "capafy-loop-daily.sh": "--loop capafy",
    ROOT / "earn" / "capafy-marketing" / "capafy-ig-marketing-daily.sh": "--loop capafy",
    ENGINE / "spawn-marketing-loop.sh": "--loop marketing-engine",
    ROOT / "self" / "self-fix.sh": "--loop $LOOP_Q",
}


class GptFirstRunnerWiringTest(unittest.TestCase):
    def test_revenue_consumers_use_shared_runner_without_provider_or_model_names(self):
        for script in CONSUMERS:
            with self.subTest(script=script):
                text = script.read_text(encoding="utf-8")
                self.assertIn("run_agent.sh", text)
                self.assertIn(f"--task-class {EXPECTED_TASK_CLASSES[script]}", text)
                self.assertIn(EXPECTED_LOOPS[script], text)
                self.assertNotRegex(text, r"command -v claude|\$CLAUDE|claude\s+-p|--model\s+sonnet")
                self.assertNotRegex(text, r"codex\s+exec|gpt-5(?:\.|-)" )

    def test_shared_runner_forwards_application_intent_escalation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args_file = root / "args.json"
            fake_runner = root / "agent-runner"
            fake_runner.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "pathlib.Path(os.environ['ARGS_FILE']).write_text(json.dumps(args))\n"
                "evidence = pathlib.Path(args[args.index('--evidence-dir') + 1])\n"
                "evidence.mkdir(parents=True, exist_ok=True)\n"
                "(evidence / 'summary.json').write_text(json.dumps({\n"
                "  'status': 'success', 'selected_provider': 'codex',\n"
                "  'selected_model': 'fixture-gpt', 'attempt_count': 1}))\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            fake_runner.chmod(0o755)
            evidence = root / "evidence"
            env = os.environ.copy()
            env.update({"AGENT_RUNNER_BIN": str(fake_runner), "ARGS_FILE": str(args_file)})
            proc = subprocess.run(
                [
                    "bash", str(RUN_AGENT), "--task-class", "application-intent-planner",
                    "--escalation-reason", "authorized fundraiser application",
                    "--evidence-dir", str(evidence), "--task-label", "capafy-fixture",
                    "--loop", "capafy",
                ],
                input="Do one bounded fixture pass.\n",
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            args = json.loads(args_file.read_text(encoding="utf-8"))
            self.assertEqual(args[args.index("--task-class") + 1], "application-intent-planner")
            self.assertEqual(args[args.index("--escalation-reason") + 1], "authorized fundraiser application")
            self.assertIn("--prompt-stdin", args)
            self.assertNotIn("--prompt-file", args)
            self.assertEqual(args[args.index("--loop") + 1], "capafy")
            self.assertNotIn("--model", args)
            self.assertFalse(any("sonnet" in arg or arg.startswith("gpt-") for arg in args))
            summary = json.loads((evidence / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["attempt_count"], 1)

    def test_shared_runner_accepts_marketing_agent_without_a_model_argument(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args_file = root / "args.json"
            fake_runner = root / "agent-runner"
            fake_runner.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "args = sys.argv[1:]\n"
                "pathlib.Path(os.environ['ARGS_FILE']).write_text(json.dumps(args))\n"
                "evidence = pathlib.Path(args[args.index('--evidence-dir') + 1])\n"
                "evidence.mkdir(parents=True, exist_ok=True)\n"
                "(evidence / 'summary.json').write_text(json.dumps({'status': 'success'}))\n",
                encoding="utf-8",
            )
            fake_runner.chmod(0o755)
            evidence = root / "evidence"
            completed = subprocess.run(
                [
                    "bash", str(RUN_AGENT), "--task-class", "marketing-agent",
                    "--evidence-dir", str(evidence), "--task-label", "larry-fixture",
                    "--loop", "larry-marketing", "--workdir", str(root),
                ],
                input="Create and publish one bounded fixture.\n",
                env={
                    **os.environ,
                    "AGENT_RUNNER_BIN": str(fake_runner),
                    "ARGS_FILE": str(args_file),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            args = json.loads(args_file.read_text(encoding="utf-8"))
            self.assertEqual(args[args.index("--task-class") + 1], "marketing-agent")
            self.assertEqual(args[args.index("--loop") + 1], "larry-marketing")
            self.assertNotIn("--model", args)

    def test_shared_output_schemas_are_strict_codex_contracts(self):
        for name in ("loop_pass.schema.json", "manifest_judgment.schema.json"):
            with self.subTest(schema=name):
                schema = json.loads((ENGINE / "schemas" / name).read_text(encoding="utf-8"))
                self.assertIs(schema["additionalProperties"], False)
                self.assertEqual(schema["properties"]["status"]["type"], "string")
                self.assertTrue(set(schema["properties"]).issubset(set(schema["required"])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
