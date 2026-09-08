import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/migrate-legacy-writer-content.py"
SPEC = importlib.util.spec_from_file_location("migrate_writer_content", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class WriterContentMigrationTest(unittest.TestCase):
    def test_cli_requires_explicit_source_root(self):
        result = subprocess.run(
            ["python3", str(SCRIPT)], capture_output=True, text=True, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--source-root", result.stderr)

    def stores(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        source = root / ".openclaw"
        target = root / "writer"
        library = source / "state/content-library"
        experience = source / "state/experience-log"
        persona = source / "skills/anicca-persona"
        library.mkdir(parents=True)
        experience.mkdir(parents=True)
        persona.mkdir(parents=True)
        target.mkdir()
        (library / "pattern-article.jsonl").write_text('{"source_id":"own"}\n')
        (library / "pattern-card-ja.jsonl").write_text('{"source_id":"card"}\n')
        (library / "account-history.jsonl").write_text('{"status":"draft"}\n')
        (library / "verbatim_blacklist.txt").write_text("borrowed phrase\n")
        (library / "unrelated.sqlite3").write_text("ignore\n")
        (library / "archive").mkdir()
        (experience / "2026-09-08.jsonl").write_text('{"useful_for_content":"y"}\n')
        (persona / "persona-anicca.md").write_text("private persona\n")
        return temporary, source, target

    def test_allowlist_preserves_content_context_without_unrelated_state(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        result = MODULE.migrate(source, target)
        self.assertEqual(result, {"copied": 6, "skipped": 0, "verified": 6, "sealed": False})
        self.assertTrue((target / "content-library/pattern-article.jsonl").is_file())
        self.assertTrue((target / "experience-log/2026-09-08.jsonl").is_file())
        self.assertEqual((target / "config/persona.md").read_text(), "private persona\n")
        self.assertFalse((target / "content-library/unrelated.sqlite3").exists())

    def test_replay_is_zero_copy_and_source_is_retained(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        MODULE.migrate(source, target)
        replay = MODULE.migrate(source, target)
        self.assertEqual(replay["copied"], 0)
        self.assertEqual(replay["skipped"], 6)
        self.assertTrue((source / "skills/anicca-persona/persona-anicca.md").is_file())

    def test_selected_symlink_fails_closed(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        selected = source / "state/content-library/pattern-article.jsonl"
        selected.unlink()
        selected.symlink_to(source / "state/content-library/unrelated.sqlite3")
        with self.assertRaisesRegex(ValueError, "regular file|symlink"):
            MODULE.migrate(source, target)


if __name__ == "__main__":
    unittest.main()
