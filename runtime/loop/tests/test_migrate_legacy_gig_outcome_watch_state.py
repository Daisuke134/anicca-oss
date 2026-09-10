import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/migrate-legacy-gig-outcome-watch-state.py"
SPEC = importlib.util.spec_from_file_location("gig_watch_migration", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GigOutcomeWatchMigrationTest(unittest.TestCase):
    def fixture(self, root: Path):
        source, target = root / "source", root / "target"
        source.mkdir()
        target.mkdir()
        (target / "events.jsonl").write_text("runtime\n")
        (source / "history.jsonl").write_text("history\n")
        (source / "domain-skills.jsonl").write_text("skills\n")
        (source / "last-sent").write_text("stamp\n")
        return source, target

    def test_precopy_replay_and_sealed_update(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.fixture(Path(directory))
            self.assertEqual(MODULE.migrate(source, target)["copied"], 3)
            self.assertEqual(MODULE.migrate(source, target)["skipped"], 3)
            (source / "history.jsonl").write_text("history\nnew\n")
            with self.assertRaisesRegex(ValueError, "sealed idle cutover"):
                MODULE.migrate(source, target)
            result = MODULE.migrate(source, target, seal=True)
            self.assertEqual(result, {"copied": 1, "skipped": 2, "verified": 3, "sealed": True})
            self.assertEqual((target / "history.jsonl").read_text(), "history\nnew\n")
            self.assertEqual((target / "events.jsonl").read_text(), "runtime\n")
            for name in (*MODULE.FILES.values(), Path("legacy-business-state-migration.json")):
                self.assertEqual((target / name).stat().st_mode & 0o777, 0o600)

    def test_missing_source_is_clean_install_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                MODULE.migrate(root / "missing", root / "target"),
                {"copied": 0, "skipped": 0, "verified": 0, "sealed": False},
            )

    def test_rejects_symlink_source_and_target_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = self.fixture(root)
            (source / "last-sent").unlink()
            (source / "last-sent").symlink_to(root / "missing")
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(source, target)
            (source / "last-sent").unlink()
            (source / "last-sent").write_text("stamp\n")
            MODULE.migrate(source, target)
            (target / "history.jsonl").write_text("foreign\n")
            with self.assertRaisesRegex(ValueError, "outside migration"):
                MODULE.migrate(source, target)


if __name__ == "__main__":
    unittest.main()
