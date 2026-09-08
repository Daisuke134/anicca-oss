import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "skills/writer-agent/scripts"))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


START = load(
    "article_daily_start_control",
    ROOT / "skills/writer-agent/scripts/article_daily_start_control.py",
)
GENERATION = load(
    "article_generation_state",
    ROOT / "skills/writer-agent/scripts/article_generation_state.py",
)
RESUME = load(
    "publication_resume",
    ROOT / "skills/writer-agent/scripts/publication_resume.py",
)
REMOTE = load(
    "publication_remote",
    ROOT / "skills/writer-agent/scripts/publication_remote.py",
)
QUARANTINE = load(
    "quarantine_invalid_run",
    ROOT / "skills/writer-agent/scripts/quarantine_invalid_run.py",
)


class ArticleStartPolicyTest(unittest.TestCase):
    def test_capacity_drain_archives_partial_provider_and_keeps_resume_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            run_id = "20260822-capacity-drain"
            run = state_root / "runs" / run_id
            run.mkdir(parents=True)
            prompt = run / "article-daily-prompt.txt"
            prompt.write_text("immutable prompt\n", encoding="utf-8")
            ledger = state_root / "articles.jsonl"
            ledger.write_text("", encoding="utf-8")
            prepared = GENERATION.initialize(run, run_id, prompt, ledger)
            GENERATION.begin(run, run_id, prompt, ledger, owner_pid=os.getpid())

            artifact = run / "article-ja.md"
            provider_pid = root / "provider.pid"
            child_pid = root / "child.pid"
            child_ready = root / "child.ready"
            provider = root / "fake-provider.py"
            child_script = root / "fake-child.py"
            child_script.write_text(
                "import pathlib, signal, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "pathlib.Path(sys.argv[1]).write_text('ready')\n"
                "while True: time.sleep(0.05)\n",
                encoding="utf-8",
            )
            provider.write_text(
                "import pathlib, signal, subprocess, sys, time\n"
                "artifact, provider_pid, child_pid, child_ready, child_script = map(pathlib.Path, sys.argv[1:])\n"
                "child = subprocess.Popen([sys.executable, str(child_script), str(child_ready)])\n"
                "provider_pid.write_text(str(__import__('os').getpid()))\n"
                "child_pid.write_text(str(child.pid))\n"
                "artifact.write_text('partial provider artifact\\n')\n"
                "while True: time.sleep(0.05)\n",
                encoding="utf-8",
            )
            stop_paths = [
                root / "disk-writers.stop",
                root / "disk-pressure.block",
            ]
            env = os.environ.copy()
            env["BOUNDED_EXEC_STOP_PATHS"] = os.pathsep.join(
                str(path) for path in stop_paths
            )
            bounded = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "runtime/loop/bounded-exec.py"),
                    "10",
                    sys.executable,
                    str(provider),
                    str(artifact),
                    str(provider_pid),
                    str(child_pid),
                    str(child_ready),
                    str(child_script),
                ],
                env=env,
            )

            def wait_for(path: Path) -> None:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if path.is_file():
                        return
                    time.sleep(0.02)
                self.fail(f"provider did not create {path}")

            def pid_gone(path: Path) -> None:
                wait_for(path)
                pid = int(path.read_text(encoding="utf-8"))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return
                    time.sleep(0.02)
                self.fail(f"process {pid} remains alive")

            def kill_if_alive(path: Path) -> None:
                if not path.is_file():
                    return
                try:
                    os.kill(int(path.read_text(encoding="utf-8")), 9)
                except ProcessLookupError:
                    pass

            try:
                wait_for(artifact)
                wait_for(provider_pid)
                wait_for(child_pid)
                wait_for(child_ready)
                stop_paths[0].write_text("drain\n", encoding="utf-8")
                self.assertEqual(bounded.wait(timeout=5), 143)
                pid_gone(provider_pid)
                pid_gone(child_pid)
            finally:
                if bounded.poll() is None:
                    bounded.kill()
                    bounded.wait()
                kill_if_alive(provider_pid)
                kill_if_alive(child_pid)

            preexisting_flag = root / "preexisting.stop"
            preexisting_flag.write_text("drain before spawn\n", encoding="utf-8")
            marker = root / "provider-started.marker"
            preexisting_env = os.environ.copy()
            preexisting_env["BOUNDED_EXEC_STOP_PATHS"] = str(preexisting_flag)
            preexisting = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "runtime/loop/bounded-exec.py"),
                    "10",
                    sys.executable,
                    "-c",
                    "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('started')",
                    str(marker),
                ],
                env=preexisting_env,
                check=False,
            )
            self.assertEqual(preexisting.returncode, 143)
            self.assertFalse(marker.exists())

            interrupted = GENERATION.archive_interrupted(
                run, run_id, prompt, ledger, 143
            )
            self.assertEqual(interrupted["status"], "interrupted-safe")
            self.assertEqual(interrupted["prompt_sha256"], prepared["prompt_sha256"])
            archive = (
                state_root
                / "interrupted-generation"
                / run_id
                / "attempt-1"
            )
            archived_artifact = archive / "article-ja.md"
            self.assertTrue(archived_artifact.is_file())
            manifest = interrupted["attempts"][-1]["archive_manifest"]
            self.assertEqual(
                manifest,
                [{
                    "path": "article-ja.md",
                    "sha256": hashlib.sha256(archived_artifact.read_bytes()).hexdigest(),
                }],
            )
            checkpoint = json.loads(
                (archive / "generation-state.json").read_text(encoding="utf-8")
            )
            receipt = json.loads(
                (archive / "generation-exhaustion-receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(checkpoint["status"], "interrupted-safe")
            self.assertEqual(
                receipt["state_sha256"],
                hashlib.sha256(
                    (archive / "generation-state.json").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                receipt["archive_manifest_sha256"],
                GENERATION.manifest_sha256(manifest),
            )
            self.assertTrue(receipt["publication_state_absent"])
            self.assertEqual(receipt["public_ledger_rows"], 0)
            decision = GENERATION.resume_decision(run, run_id, prompt, ledger)
            self.assertTrue(decision["resumable"])
            self.assertEqual(decision["status"], "interrupted-safe")
            self.assertEqual(prompt.read_text(encoding="utf-8"), "immutable prompt\n")
            self.assertFalse((run / "gates" / "publication-state.json").exists())
            self.assertEqual(ledger.read_text(encoding="utf-8"), "")

            wrapper = (
                ROOT / "skills/writer-agent/article-daily.sh"
            ).read_text(encoding="utf-8")
            run_model = wrapper[wrapper.index("run_model_pass()") : wrapper.index(
                "# AUTH FAILURE SAFETY"
            )]
            self.assertEqual(run_model.count("BOUNDED_EXEC_STOP_PATHS="), 1)
            self.assertIn(
                'BOUNDED_EXEC_STOP_PATHS="${LIFE_MANAGER_HOST_STATE_DIR:-$HOME/.local/state/life-manager/state}/disk-writers.stop"',
                run_model,
            )

    def test_signal_drains_model_and_broker_before_interruption_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider_pid = root / "provider.pid"
            child_pid = root / "child.pid"
            broker_pid = root / "broker.pid"
            archive = root / "archive.marker"
            prompt = root / "prompt.txt"
            prompt.write_text("test\n", encoding="utf-8")
            (root / "run.log").write_text("", encoding="utf-8")
            runner = root / "runner.py"
            runner.write_text(
                "#!/usr/bin/env python3\n"
                "import os, pathlib, signal, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
                "pathlib.Path(os.environ['PROVIDER_PID']).write_text(str(os.getpid()))\n"
                "pathlib.Path(os.environ['CHILD_PID']).write_text(str(child.pid))\n"
                "while True: time.sleep(.05)\n",
                encoding="utf-8",
            )
            runner.chmod(0o755)
            broker = root / "broker.py"
            broker.write_text(
                "import os, pathlib, signal, time\n"
                "pathlib.Path(os.environ['BROKER_PID']).write_text(str(os.getpid()))\n"
                "signal.signal(signal.SIGTERM, lambda *_: exit(0))\n"
                "while True: time.sleep(.05)\n",
                encoding="utf-8",
            )
            state = root / "state.py"
            state.write_text(
                "import os, pathlib, sys, time\n"
                "def alive(pid):\n"
                " try: os.kill(pid, 0); return True\n"
                " except ProcessLookupError: return False\n"
                "if 'archive-interrupted' in sys.argv:\n"
                " deadline=time.monotonic()+3\n"
                " paths=[pathlib.Path(os.environ['PROVIDER_PID']), pathlib.Path(os.environ['CHILD_PID']), pathlib.Path(os.environ['BROKER_PID'])]\n"
                " while time.monotonic()<deadline and any(alive(int(p.read_text())) for p in paths): time.sleep(.02)\n"
                " pathlib.Path(os.environ['ARCHIVE_MARKER']).write_text('alive' if any(alive(int(p.read_text())) for p in paths) else 'drained')\n",
                encoding="utf-8",
            )
            wrapper = (
                ROOT / "skills/writer-agent/article-daily.sh"
            ).read_text(encoding="utf-8")
            start = wrapper.index('MODEL_PASS_PID=""\ndrain_generation_workers()')
            end = wrapper.index(
                'python3 "$GENERATION_STATE" "${GENERATION_ARGS[@]}" begin',
                start,
            )
            normal_start = wrapper.index("run_model_pass\nRC=$?", end)
            normal_end = wrapper.index("trap - INT TERM\nPASS_OUTPUT=", normal_start)
            harness = root / "harness.sh"
            prefix = (
                "#!/usr/bin/env bash\nset -uo pipefail\n"
                f"ARTICLE_ROOT={ROOT / 'skills/writer-agent'}\n"
                "ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS=30\n"
                f"ARTICLE_MODEL_RUNNER={runner}\nPROMPT_FILE={prompt}\n"
                f"LOG={root / 'run.log'}\nGENERATION_STATE={state}\n"
                "RUN_TS=test\nGENERATION_ARGS=(--fixture test)\nGENERATION_ATTEMPT_ACTIVE=1\n"
                f"python3 {broker} &\nJUDGE_BROKER_PID=$!\n"
            )
            harness.write_text(
                prefix + wrapper[start:end] + "run_model_pass\nexit $?\n",
                encoding="utf-8",
            )
            harness.chmod(0o755)
            env = os.environ.copy()
            env.update({
                "PROVIDER_PID": str(provider_pid),
                "CHILD_PID": str(child_pid),
                "BROKER_PID": str(broker_pid),
                "ARCHIVE_MARKER": str(archive),
            })
            process = subprocess.Popen([str(harness)], env=env)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (
                child_pid.is_file() and broker_pid.is_file()
            ):
                time.sleep(.02)
            self.assertTrue(child_pid.is_file())
            self.assertTrue(broker_pid.is_file())
            process.terminate()
            self.assertEqual(process.wait(timeout=8), 143)
            self.assertEqual(archive.read_text(encoding="utf-8"), "drained")

            for path in (provider_pid, child_pid, broker_pid, archive):
                path.unlink()
            harness.write_text(
                prefix.replace(
                    "ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS=30",
                    "ARTICLE_MODEL_AGENT_TIMEOUT_SECONDS=.2",
                )
                + wrapper[start:end]
                + wrapper[normal_start:normal_end]
                + 'exit "$RC"\n',
                encoding="utf-8",
            )
            completed = subprocess.run([str(harness)], env=env, timeout=8)
            self.assertEqual(completed.returncode, 124)
            self.assertEqual(archive.read_text(encoding="utf-8"), "drained")

    def _duplicate_media_run(self, root: Path, *, live: bool = False, status: str | None = None):
        run = root / "runs" / "daily-2026-08-21"
        gates = run / "gates"
        gates.mkdir(parents=True)
        headline = run / "headline-image.png"
        body = run / "body-diagram.png"
        headline.write_bytes(b"same-media")
        body.write_bytes(b"same-media")
        state_path = gates / "publication-state.json"
        pairs = {
            f"{platform}/{lang}": {"status": "unavailable"}
            for platform, lang in START.ACTIVE_REQUIRED
        }
        pairs["note/ja"]["status"] = status or ("live" if live else "unavailable")
        state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "publication_contract": "active-four",
                    "run_id": run.name,
                    "run_dir": str(run.resolve()),
                    "state_path": str(state_path.resolve()),
                    "ledger_path": str((root / "articles.jsonl").resolve()),
                    "media": {
                        "headline_image": {"path": str(headline)},
                        "body_assets": [{"path": str(body)}],
                    },
                    "pairs": pairs,
                }
            ),
            encoding="utf-8",
        )
        (root / "articles.jsonl").write_text("", encoding="utf-8")
        return run

    def test_duplicate_media_quarantine_releases_same_day_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self._duplicate_media_run(root)
            receipt = QUARANTINE.quarantine(root, run.name)
            with patch.object(START, "validated_live_set", return_value=(False, None)):
                decision = START.decide(root, "2026-08-21")
        self.assertEqual(receipt["reason"], "duplicate-media")
        self.assertEqual(decision["action"], "new")
        self.assertEqual(decision["reason"], "same-jst-day-invalid-media-proof")

    def test_quarantine_refuses_live_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self._duplicate_media_run(root, live=True)
            with self.assertRaises(QUARANTINE.QuarantineError):
                QUARANTINE.quarantine(root, run.name)

    def test_quarantine_refuses_ambiguous_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self._duplicate_media_run(root, status="ambiguous")
            with self.assertRaises(QUARANTINE.QuarantineError):
                QUARANTINE.quarantine(root, run.name)
            with patch.object(START, "validated_live_set", return_value=(False, None)):
                self.assertEqual(START.decide(root, "2026-08-21")["action"], "block-incomplete")

    def test_terminalize_invalid_pair_under_shared_lock_then_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self._duplicate_media_run(root)
            state_path = run / "gates" / "publication-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["pairs"]["x-article/ja"] = {
                "status": "intent",
                "target": "https://x.example/draft/1",
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            before_ledger = (root / "articles.jsonl").read_bytes()
            entry = QUARANTINE.terminalize_pair(
                root, run.name, "x-article/ja", "duplicate-media-quarantine"
            )
            receipt = QUARANTINE.quarantine(root, run.name)
            after = json.loads(state_path.read_text(encoding="utf-8"))
            after_ledger = (root / "articles.jsonl").read_bytes()
        self.assertEqual(entry["status"], "unavailable")
        self.assertEqual(after["pairs"]["x-article/ja"]["target"], "https://x.example/draft/1")
        self.assertEqual(after_ledger, before_ledger)
        self.assertEqual(receipt["reason"], "duplicate-media")

    def test_quarantine_rejects_ambiguous_same_run_ledger_publication_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self._duplicate_media_run(root)
            (root / "articles.jsonl").write_text(
                json.dumps({"run_id": run.name, "published": "false"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(QUARANTINE.QuarantineError):
                QUARANTINE.quarantine(root, run.name)

    def test_quarantine_receipt_tamper_does_not_authorize_or_block_fresh_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self._duplicate_media_run(root)
            QUARANTINE.quarantine(root, run.name)
            receipt = run / "gates" / "run-quarantine.json"
            tampered = json.loads(receipt.read_text(encoding="utf-8"))
            tampered["created_at"] = "forged"
            receipt.write_text(json.dumps(tampered), encoding="utf-8")
            self.assertFalse(QUARANTINE.receipt_is_valid(run, run.name))
            with patch.object(START, "validated_live_set", return_value=(False, None)):
                self.assertEqual(START.decide(root, "2026-08-21")["action"], "new")

    def test_quarantine_gates_symlink_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = self._duplicate_media_run(root)
            shutil.rmtree(run / "gates")
            outside = root / "outside"
            outside.mkdir()
            (run / "gates").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(QUARANTINE.QuarantineError):
                QUARANTINE.quarantine(root, run.name)

    def test_completed_active_four_releases_new_run_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            run = state / "runs" / "daily-2026-08-21"
            (run / "gates").mkdir(parents=True)
            state_path = run / "gates" / "publication-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "publication_contract": "active-four",
                        "run_id": run.name,
                        "state_path": str(state_path),
                        "ledger_path": str(state / "articles.jsonl"),
                    }
                ),
                encoding="utf-8",
            )

            def live_set(_rows, _run_id, required):
                return (required == START.ACTIVE_REQUIRED, "topic-1")

            with patch.object(START, "validated_live_set", side_effect=live_set):
                decision = START.decide(state, "2026-08-21")

        self.assertEqual(decision["action"], "new")
        self.assertEqual(decision["run_id"], "")
        self.assertEqual(
            decision["reason"], "new-after-complete:active-four"
        )

    def test_exhausted_prepublication_archive_releases_new_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            run = state / "runs" / "20260821-072939"
            (run / "gates" / "judge-broker").mkdir(parents=True)
            (run / "gates" / "judge-broker" / "heartbeat").write_text("x")
            archive = state / "interrupted-generation" / run.name / "attempt-4"
            (archive / "gates").mkdir(parents=True)
            for relative in ("gates/attempt-budget-state.json",):
                path = archive / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("archived", encoding="utf-8")
            manifest = [
                {"path": relative, "sha256": hashlib.sha256(
                    (archive / relative).read_bytes()
                ).hexdigest()}
                for relative in ("gates/attempt-budget-state.json",)
            ]
            state_value = {
                "version": 1,
                "run_id": run.name,
                "prompt_sha256": "a" * 64,
                "status": "interrupted-safe",
                "maximum_attempts": 3,
                "maximum_empty_interruption_recoveries": 1,
                "attempts": [
                    {"attempt": 1, "status": "interrupted-safe", "return_code": 143, "archive_manifest": []},
                    {"attempt": 2, "status": "interrupted-safe", "return_code": 143, "archive_manifest": manifest},
                    {"attempt": 3, "status": "interrupted-safe", "return_code": 143, "archive_manifest": manifest},
                    {"attempt": 4, "status": "interrupted-safe", "return_code": 124, "archive_manifest": manifest},
                ],
            }
            state_path = archive / "generation-state.json"
            receipt_path = archive / "generation-exhaustion-receipt.json"

            def write_proof(value):
                state_value["attempts"][-1]["archive_manifest"] = value
                state_path.write_text(json.dumps(state_value), encoding="utf-8")
                receipt_path.write_text(json.dumps({
                    "schema": "writer.generation-exhaustion-receipt",
                    "version": 1, "run_id": run.name, "attempt": 4,
                    "status": "interrupted-safe", "return_code": 124,
                    "charged_attempts": 3, "maximum_attempts": 3,
                    "state_sha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
                    "archive_manifest_sha256": hashlib.sha256(json.dumps(
                        value, sort_keys=True, separators=(",", ":")
                    ).encode()).hexdigest(),
                    "publication_state_absent": True, "public_ledger_rows": 0,
                }), encoding="utf-8")

            write_proof(manifest)
            (state / "articles.jsonl").write_text("", encoding="utf-8")

            with patch.object(START, "validated_live_set", return_value=(False, None)):
                decision = START.decide(state, "2026-08-21")
                write_proof([{}])
                malformed = START.decide(state, "2026-08-21")
                partial_path = archive / "article-ja.md"
                partial_path.write_text("partial", encoding="utf-8")
                write_proof([{
                    "path": "article-ja.md",
                    "sha256": hashlib.sha256(partial_path.read_bytes()).hexdigest(),
                }])
                partial = START.decide(state, "2026-08-21")
                write_proof([{
                    "path": "./article-ja.md",
                    "sha256": hashlib.sha256(partial_path.read_bytes()).hexdigest(),
                }])
                alias = START.decide(state, "2026-08-21")
                for relative in (
                    "article-en.md", "headline-image.png", "body-diagram.png"
                ):
                    (archive / relative).write_text("unlisted", encoding="utf-8")
                write_proof([{
                    "path": "article-ja.md",
                    "sha256": hashlib.sha256(partial_path.read_bytes()).hexdigest(),
                }])
                unlisted = START.decide(state, "2026-08-21")

        self.assertEqual(decision["action"], "new")
        self.assertEqual(decision["run_id"], "")
        self.assertEqual(
            decision["reason"], "same-jst-day-exhausted-prepublication-archive"
        )
        self.assertEqual(malformed["reason"], "same-jst-day-unclassified-run")
        self.assertEqual(partial["reason"], "same-jst-day-unclassified-run")
        self.assertEqual(alias["reason"], "same-jst-day-unclassified-run")
        self.assertEqual(unlisted["reason"], "same-jst-day-unclassified-run")

    def test_legacy_exact8_partial_active_subset_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            run = state / "runs" / "daily-2026-08-21"
            (run / "gates").mkdir(parents=True)
            state_path = run / "gates" / "publication-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "publication_contract": "legacy-exact8",
                        "run_id": run.name,
                        "state_path": str(state_path),
                        "ledger_path": str(state / "articles.jsonl"),
                    }
                ),
                encoding="utf-8",
            )

            def live_set(_rows, _run_id, required):
                return (required == START.ACTIVE_REQUIRED, "topic-1")

            with patch.object(START, "validated_live_set", side_effect=live_set), patch.object(
                START, "publication_plan", return_value={"resumable": True}
            ):
                decision = START.decide(state, "2026-08-21")

        self.assertNotEqual(decision["action"], "new")
        self.assertEqual(decision["action"], "skip-pending-worker")

    def test_pending_active_four_remains_resume_worker_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            run = state / "runs" / "daily-2026-08-21"
            (run / "gates").mkdir(parents=True)
            state_path = run / "gates" / "publication-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "publication_contract": "active-four",
                        "run_id": run.name,
                        "state_path": str(state_path),
                        "ledger_path": str(state / "articles.jsonl"),
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(START, "validated_live_set", return_value=(False, None)), patch.object(
                START, "publication_plan", return_value={"resumable": True}
            ):
                decision = START.decide(state, "2026-08-21")

        self.assertEqual(decision["action"], "skip-pending-worker")

    def _x_readability_release_fixture(self, root: Path):
        run = root / "runs" / "20260821-103056"
        repair = run / "gates" / "x-inplace-repair" / "ja"
        repair.mkdir(parents=True)
        body = run / "body.png"
        Image.new("RGB", (1300, 70), "white").save(body, format="PNG")
        body_path = str(body.resolve())
        body_sha = hashlib.sha256(body.read_bytes()).hexdigest()
        live_urls = {
            "note/ja": "https://note.com/anicca123/n/n1",
            "substack/ja": "https://aniccabuddha.substack.com/p/j1",
            "substack/en": "https://aniccaai2026.substack.com/p/e1",
        }
        pairs = {
            pair: {
                "status": "live",
                "receipt": {"live_url": url, "evidence": {}},
            }
            for pair, url in live_urls.items()
        }
        pairs["x-article/ja"] = {
            "platform": "x-article",
            "lang": "ja",
            "status": "unavailable",
            "target_kind": "x-draft-url",
            "target": "https://x.com/compose/articles/edit/2090758197418291200",
            "error": "x-article body media readability failed: too-flat:body",
        }
        state = {
            "publication_contract": "active-four",
            "run_id": run.name,
            "topic_id": "topic-1",
            "destination_identities": {
                "note/ja": "anicca123",
                "substack/ja": "aniccabuddha.substack.com",
                "substack/en": "aniccaai2026.substack.com",
                "x-article/ja": "diceai0",
            },
            "drafts": {"ja": {"sha256": "a" * 64}, "en": {"sha256": "b" * 64}},
            "pairs": pairs,
            "media": {"body_assets": [{"path": str(body), "sha256": body_sha}]},
        }
        state_path = run / "gates" / "publication-state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        readability = {
            "version": 1,
            "status": "FAIL",
            "run_id": run.name,
            "pair": "x-article/ja",
            "target": pairs["x-article/ja"]["target"],
            "target_kind": "x-draft-url",
            "readback_status": "not-live",
            "readback_verified": True,
            "content_verified": True,
            "artifact_sha256": "a" * 64,
            "destination_identity": "diceai0",
            "identity_verified": True,
            "identity_source": "x-authenticated-edit-url",
            "render_width": 587,
            "min_height": 110,
            "max_height": 650,
            "violations": [f"too-flat:{body_path}:source=1300x70:projected=31.61:min=110"],
            "images": [{
                "path": body_path, "sha256": body_sha, "width": 1300,
                "height": 70, "projected_height": 31.61,
            }],
        }
        (repair / "media-readability.json").write_text(
            json.dumps(readability), encoding="utf-8"
        )
        rows = [
            {
                "run_id": run.name,
                "topic_id": "topic-1",
                "platform": pair.split("/", 1)[0],
                "lang": pair.split("/", 1)[1],
                "published": True,
                "reality_gate": "PASS",
                "live_url": url,
            }
            for pair, url in live_urls.items()
        ]
        for row in rows:
            pair = f"{row['platform']}/{row['lang']}"
            row.update(
                {
                    "verified": True,
                    "public_id": pair,
                    "published_at": "2026-08-21T12:00:00Z",
                    "content_verified": True,
                    "asset_verified": True,
                    "body_media_verified": True,
                    "destination_identity": state["destination_identities"][pair],
                    "identity_verified": True,
                    "identity_source": "test-remote-readback",
                }
            )
        (root / "articles.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
        return run, state, rows

    @staticmethod
    def _fake_remote_probe(pair, target, state):
        if pair == "x-article/ja":
            return {
                "status": "not-live",
                "verified": True,
                "target": target,
                "content_verified": True,
                "artifact_sha256": state["drafts"]["ja"]["sha256"],
                "destination_identity": "diceai0",
                "identity_verified": True,
                "identity_source": "x-authenticated-edit-url",
            }
        entry = state["pairs"][pair]
        result = {
            "status": "live",
            "verified": True,
            "live_url": entry["receipt"]["live_url"],
            "public_id": pair,
            "published_at": "2026-08-21T12:00:00Z",
            "content_verified": True,
            "asset_verified": True,
            "body_media_verified": True,
            "destination_identity": state["destination_identities"][pair],
            "identity_verified": True,
            "identity_source": "test-remote-readback",
        }
        if pair == "note/ja":
            result.update({"monetization_verified": True, "price": 500})
        else:
            result.update(
                {
                    "monetization_verified": True,
                    "audience": "only_paid",
                    "paywall_verified": True,
                }
            )
        return result

    def test_unavailable_x_readability_proof_releases_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, state, rows = self._x_readability_release_fixture(root)
            with patch.dict(sys.modules, {"publication_resume": RESUME, "publication_remote": REMOTE}), patch.object(
                RESUME.PublicationStore,
                "validate_managed_boundary",
                return_value=state,
            ), patch.object(RESUME, "validate_receipt_evidence"), patch.object(
                REMOTE, "probe", side_effect=self._fake_remote_probe
            ), patch.object(
                START, "validated_live_set", return_value=(False, None)
            ), patch.object(START, "proof", side_effect=START.QuarantineError("not duplicate")):
                decision = START.decide(root, "2026-08-21")
        self.assertEqual(decision["action"], "new")
        self.assertEqual(decision["reason"], "same-jst-day-unavailable-x-readability")

    def test_unavailable_x_readability_tamper_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, state, rows = self._x_readability_release_fixture(root)
            rows[0]["effect"] = 1
            rows[1]["payout"] = 1
            (root / "articles.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            with patch.dict(sys.modules, {"publication_resume": RESUME, "publication_remote": REMOTE}), patch.object(
                RESUME.PublicationStore,
                "validate_managed_boundary",
                return_value=state,
            ), patch.object(RESUME, "validate_receipt_evidence"), patch.object(
                REMOTE, "probe", side_effect=self._fake_remote_probe
            ), patch.object(
                START, "validated_live_set", return_value=(False, None)
            ), patch.object(START, "proof", side_effect=START.QuarantineError("not duplicate")), patch.object(
                START, "publication_plan", return_value={"resumable": True}
            ):
                decision = START.decide(root, "2026-08-21")
        self.assertEqual(decision["action"], "skip-pending-worker")

    def test_unavailable_x_readability_duplicate_live_row_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, state, rows = self._x_readability_release_fixture(root)
            rows.append(dict(rows[0]))
            (root / "articles.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            with patch.dict(sys.modules, {"publication_resume": RESUME, "publication_remote": REMOTE}), patch.object(
                RESUME.PublicationStore,
                "validate_managed_boundary",
                return_value=state,
            ), patch.object(RESUME, "validate_receipt_evidence"), patch.object(
                REMOTE, "probe", side_effect=self._fake_remote_probe
            ), patch.object(
                START, "validated_live_set", return_value=(False, None)
            ), patch.object(START, "proof", side_effect=START.QuarantineError("not duplicate")), patch.object(
                START, "publication_plan", return_value={"resumable": True}
            ):
                decision = START.decide(root, "2026-08-21")
        self.assertEqual(decision["action"], "skip-pending-worker")

    def test_unavailable_x_readability_unknown_state_effect_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, state, rows = self._x_readability_release_fixture(root)
            state["pairs"]["x-article/ja"]["effect"] = 1
            (run / "gates" / "publication-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            with patch.dict(sys.modules, {"publication_resume": RESUME, "publication_remote": REMOTE}), patch.object(
                RESUME.PublicationStore,
                "validate_managed_boundary",
                return_value=state,
            ), patch.object(RESUME, "validate_receipt_evidence"), patch.object(
                REMOTE, "probe", side_effect=self._fake_remote_probe
            ), patch.object(
                START, "validated_live_set", return_value=(False, None)
            ), patch.object(START, "proof", side_effect=START.QuarantineError("not duplicate")), patch.object(
                START, "publication_plan", return_value={"resumable": True}
            ):
                decision = START.decide(root, "2026-08-21")
        self.assertEqual(decision["action"], "skip-pending-worker")

    def test_unavailable_x_readability_target_rebind_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, state, rows = self._x_readability_release_fixture(root)
            state["pairs"]["x-article/ja"]["target"] = (
                "https://x.com/compose/articles/edit/2090758197418291201"
            )
            (run / "gates" / "publication-state.json").write_text(
                json.dumps(state), encoding="utf-8"
            )
            with patch.dict(sys.modules, {"publication_resume": RESUME, "publication_remote": REMOTE}), patch.object(
                RESUME.PublicationStore,
                "validate_managed_boundary",
                return_value=state,
            ), patch.object(RESUME, "validate_receipt_evidence"), patch.object(
                REMOTE, "probe", side_effect=self._fake_remote_probe
            ), patch.object(
                START, "validated_live_set", return_value=(False, None)
            ), patch.object(START, "proof", side_effect=START.QuarantineError("not duplicate")), patch.object(
                START, "publication_plan", return_value={"resumable": True}
            ):
                decision = START.decide(root, "2026-08-21")
        self.assertEqual(decision["action"], "skip-pending-worker")

    def test_unavailable_x_readability_monetization_drift_stays_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, state, rows = self._x_readability_release_fixture(root)

            def drift_probe(pair, target, current_state):
                result = self._fake_remote_probe(pair, target, current_state)
                if pair == "note/ja":
                    result["monetization_verified"] = False
                    result["price"] = 0
                return result

            with patch.dict(sys.modules, {"publication_resume": RESUME, "publication_remote": REMOTE}), patch.object(
                RESUME.PublicationStore,
                "validate_managed_boundary",
                return_value=state,
            ), patch.object(RESUME, "validate_receipt_evidence"), patch.object(
                REMOTE, "probe", side_effect=drift_probe
            ), patch.object(
                START, "validated_live_set", return_value=(False, None)
            ), patch.object(START, "proof", side_effect=START.QuarantineError("not duplicate")), patch.object(
                START, "publication_plan", return_value={"resumable": True}
            ):
                decision = START.decide(root, "2026-08-21")
        self.assertEqual(decision["action"], "skip-pending-worker")

    def test_malformed_ledger_blocks_before_same_day_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, _state, _rows = self._x_readability_release_fixture(Path(tmp))
            (root / "articles.jsonl").write_text('{"published":true}\nnot-json\n', encoding="utf-8")
            decision = START.decide(root, "2026-08-21")
        self.assertEqual(decision, {"action": "block-incomplete", "run_id": "", "reason": "ledger-invalid"})

    def test_x_remote_fallback_cannot_self_assert_authenticated_draft(self):
        source = (ROOT / "skills/writer-agent/scripts/publication_remote.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("publish_buttons", source)

    def test_quality_advisory_run_releases_bounded_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for run_id in ("20260821-100000", "20260821-130847"):
                (root / "runs" / run_id).mkdir(parents=True)
            with patch.object(START, "validated_live_set", return_value=(False, None)), patch.object(
                START, "proof", side_effect=START.QuarantineError("not duplicate")
            ), patch.object(
                START,
                "generation_resume_plan",
                return_value={"resumable": False},
            ), patch.object(
                START,
                "terminal_quality_finished_at",
                return_value=(object(), "topic-1", "explainer", {}),
            ):
                decision = START.decide(root, "2026-08-21")
        self.assertEqual(decision["action"], "skip-quality-miss")

    def _quality_advisory_fixture(self, root: Path):
        run = root / "runs" / "20260821-130847"
        gates = run / "gates"
        gates.mkdir(parents=True)
        hashes = {}
        for lang in ("ja", "en"):
            article = run / f"article-{lang}.md"
            article.write_text(f"# {lang}\nbody\n", encoding="utf-8")
            hashes[lang] = hashlib.sha256(article.read_bytes()).hexdigest()
            (gates / f"editorial-{lang}.json").write_text(
                json.dumps({"article_sha256": hashes[lang], "fixes": ["fix"]}),
                encoding="utf-8",
            )
            (gates / f"reader-testing-gate-{lang}.terminal.json").write_text(
                json.dumps({
                    "article_sha256": hashes[lang],
                    "payload": {"unanswered_questions": ["question"]},
                }),
                encoding="utf-8",
            )
            (gates / f"identity-{lang}.json").write_text(
                json.dumps({"verdict": "PASS", "article_sha256": hashes[lang]}),
                encoding="utf-8",
            )
            (gates / f"conscience-{lang}.json").write_text(
                json.dumps({"verdict": "ALLOW", "reasons": []}),
                encoding="utf-8",
            )
        quality = {
            "version": 2,
            "attempt": 1,
            "action": "ready_to_freeze",
            "quality_advisory": True,
            "publication_policy": "continuous",
            "failed_languages": ["ja", "en"],
            "quality": {
                lang: {
                    "article_sha256": hashes[lang],
                    "editorial": "FAIL",
                    "reader": "FAIL",
                    "identity": "PASS",
                    "evaluation_current": True,
                    "identity_current": True,
                    "ready": False,
                }
                for lang in ("ja", "en")
            },
        }
        (gates / "quality-self-heal.json").write_text(json.dumps(quality), encoding="utf-8")
        (gates / "generation-state.json").write_text(
            json.dumps({
                "version": 1,
                "run_id": run.name,
                "status": "provider-returned",
                "attempts": [{
                    "status": "provider-returned",
                    "return_code": 0,
                    "finished_at": "2026-08-21T12:00:00Z",
                }],
            }),
            encoding="utf-8",
        )
        (gates / "topic-route.json").write_text(
            json.dumps({"topic_id": "topic-1", "editorial_form": "explainer"}),
            encoding="utf-8",
        )
        return run, gates

    def test_quality_advisory_release_requires_conscience_and_identity_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, gates = self._quality_advisory_fixture(root)
            self.assertIsNotNone(START.terminal_quality_finished_at(run, run.name, []))
            (gates / "conscience-ja.json").write_text(
                json.dumps({"verdict": "BLOCK", "reasons": ["bad"]}), encoding="utf-8"
            )
            self.assertIsNone(START.terminal_quality_finished_at(run, run.name, []))
            (gates / "conscience-ja.json").write_text(
                json.dumps({"verdict": "ALLOW", "reasons": []}), encoding="utf-8"
            )
            (gates / "identity-en.json").write_text(
                json.dumps({"verdict": "PASS", "article_sha256": "0" * 64}), encoding="utf-8"
            )
            self.assertIsNone(START.terminal_quality_finished_at(run, run.name, []))

    def test_quality_advisory_release_rejects_dangling_publication_state_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run, gates = self._quality_advisory_fixture(root)
            (gates / "publication-state.json").symlink_to(gates / "missing-state.json")
            self.assertIsNone(START.terminal_quality_finished_at(run, run.name, []))

    def test_remote_live_finalize_rejects_monetization_drift(self):
        result = REMOTE.finalize_live(
            {},
            "note/ja",
            "nb-test",
            {
                "status": "live",
                "verified": True,
                "content_verified": True,
                "monetization_verified": False,
            },
        )
        self.assertEqual(result["reason"], "note-monetization-readback-failed")

    def test_invalid_publication_state_contract_never_releases_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            run = state / "runs" / "daily-2026-08-21"
            (run / "gates").mkdir(parents=True)
            (run / "gates" / "publication-state.json").write_text(
                json.dumps(
                    {
                        "version": 999,
                        "publication_contract": "active-four",
                        "run_id": "wrong-run",
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(START, "validated_live_set", return_value=(True, "topic-1")), patch.object(
                START, "publication_plan", return_value={"resumable": True}
            ):
                decision = START.decide(state, "2026-08-21")

        self.assertEqual(decision["action"], "skip-pending-worker")

    def test_published_hashes_are_identity_scoped(self):
        digest = "a" * 64
        rows = [
            {"published": True, "lang": "ja", "artifact_sha256": digest},
            {"published": False, "lang": "en", "artifact_sha256": digest},
            {"published": True, "lang": "ja", "artifact_sha256": "invalid"},
        ]
        self.assertEqual(RESUME.PublicationStore._published_artifact_hashes(rows), {("ja", digest)})

    def test_existing_state_rechecks_cross_run_hash_at_publish_boundary(self):
        digest = "b" * 64
        store = RESUME.PublicationStore.__new__(RESUME.PublicationStore)
        store._ledger_rows_locked = lambda: [
            {
                "run_id": "daily-2026-08-20",
                "published": True,
                "lang": "ja",
                "artifact_sha256": digest,
            }
        ]
        state = {
            "run_id": "daily-2026-08-21",
            "pairs": {"note/ja": {"lang": "ja"}},
            "drafts": {"ja": {"sha256": digest}},
        }
        with self.assertRaises(RESUME.InvariantError):
            store._assert_no_duplicate_published_artifact_locked(state, "note/ja")


if __name__ == "__main__":
    unittest.main()
