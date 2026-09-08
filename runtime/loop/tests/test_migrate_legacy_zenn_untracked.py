import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/migrate-legacy-zenn-untracked.py"
SPEC = importlib.util.spec_from_file_location("migrate_zenn_untracked", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class LegacyZennUntrackedMigrationTest(unittest.TestCase):
    def stores(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        source = root / "legacy-zenn"
        target = root / "writer"
        subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
        (source / "articles").mkdir()
        (source / "articles/tracked.md").write_text("tracked\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(source), "add", "articles/tracked.md"], check=True)
        (source / "articles/draft.md").write_text("draft\n", encoding="utf-8")
        (source / ".writer-retries").mkdir()
        (source / ".writer-retries/job.json").write_text("{}\n", encoding="utf-8")
        target.mkdir()
        return temporary, source, target

    def test_only_untracked_files_are_preserved_outside_checkout(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        result = MODULE.migrate(source, target)
        self.assertEqual(result, {"copied": 2, "skipped": 0, "verified": 2, "sealed": False})
        archive = target / "legacy-zenn-untracked"
        self.assertEqual((archive / "articles/draft.md").read_text(), "draft\n")
        self.assertEqual((archive / ".writer-retries/job.json").read_text(), "{}\n")
        self.assertFalse((archive / "articles/tracked.md").exists())
        self.assertFalse((target / "checkouts").exists())
        self.assertEqual(MODULE.migrate(source, target)["skipped"], 2)

    def test_untracked_symlink_is_refused(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        (source / "link").symlink_to(source / "articles/draft.md")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            MODULE.migrate(source, target)


if __name__ == "__main__":
    unittest.main()
