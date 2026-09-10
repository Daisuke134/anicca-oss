import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "migrate_self_improve", ROOT / "runtime/migrate-legacy-self-improve-state.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SelfImproveMigrationTest(unittest.TestCase):
    def test_copies_only_self_improve_and_replay_is_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "legacy", root / "canonical"
            (source / "state").mkdir(parents=True)
            old_evidence = source / "evidence/runs/2026-01-01/self-improve/wanted/execution.json"
            rows = [
                {"runner_id": "clip", "run_id": "other"},
                {"runner_id": "self-improve", "run_id": "wanted",
                 "evidence": [{"path": str(old_evidence)}],
                 "effects": [{"evidence": str(old_evidence)}],
                 "metrics": [{"evidence": str(old_evidence)}]},
            ]
            for name in ("run-reports.jsonl", "run-deliveries.jsonl"):
                (source / "state" / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
            evidence = source / "evidence/runs/2026-01-01/self-improve/wanted"
            evidence.mkdir(parents=True)
            (evidence / "execution.json").write_text("{}\n")
            (source / "logs").mkdir()
            (source / "logs/self-improve-evolve-launchd.out.log").write_text("old log\n")

            self.assertEqual(MODULE.migrate(source, target), {"copied_rows": 2, "copied_files": 2})
            self.assertEqual(MODULE.migrate(source, target), {"copied_rows": 0, "copied_files": 0})
            for name in ("run-reports.jsonl", "run-deliveries.jsonl"):
                body = (target / "state" / name).read_text()
                self.assertIn('"runner_id":"self-improve"', body)
                self.assertNotIn('"runner_id":"clip"', body)
                self.assertNotIn(str(source), body)
                self.assertEqual(body.count(str(target / "evidence/runs")), 3)
                self.assertEqual((target / "state" / name).stat().st_mode & 0o777, 0o600)

            (target / "evidence/runs/2026-01-01/self-improve/wanted/execution.json").write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "conflicting canonical evidence"):
                MODULE.migrate(source, target)

    def test_rejects_symlink_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(link, root / "target")

    def test_rejects_intermediate_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, outside = root / "source", root / "target", root / "outside"
            source.mkdir()
            outside.mkdir()
            (source / "state").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(source, target)

            source_state = source / "state"
            source_state.unlink()
            source_state.mkdir()
            (source / "evidence/runs").mkdir(parents=True)
            (target / "state").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(source, target)

    def test_rejects_conflicting_source_run_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "state").mkdir(parents=True)
            (source / "evidence/runs").mkdir(parents=True)
            rows = [
                {"runner_id": "self-improve", "run_id": "same", "status": "success"},
                {"runner_id": "self-improve", "run_id": "same", "status": "failed"},
            ]
            (source / "state/run-reports.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
            )
            with self.assertRaisesRegex(ValueError, "conflicting legacy receipt"):
                MODULE.migrate(source, root / "target")


if __name__ == "__main__":
    unittest.main()
