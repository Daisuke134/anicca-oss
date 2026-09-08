import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/migrate-legacy-writer-note-work.py"
SPEC = importlib.util.spec_from_file_location("migrate_writer_note_work", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class WriterNoteWorkMigrationTest(unittest.TestCase):
    def test_only_live_allowlist_is_copied_and_replay_is_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "legacy"
            target = root / "writer/note-work"
            source.mkdir()
            for name in MODULE.LIVE_FILES:
                (source / name).write_text(name, encoding="utf-8")
            (source / ".PUBLISH_ENABLED").write_text("", encoding="utf-8")
            (source / "thumb.png").write_bytes(b"ephemeral-cover")
            (source / "old-assets").mkdir()
            (source / "old-assets/image.png").write_bytes(b"old")
            first = MODULE.migrate(source, target)
            self.assertEqual(first["copied"], len(MODULE.LIVE_FILES))
            self.assertTrue(all((target / name).is_file() for name in MODULE.LIVE_FILES))
            self.assertFalse((target / ".PUBLISH_ENABLED").exists())
            self.assertFalse((target / "thumb.png").exists())
            self.assertFalse((target / "old-assets").exists())
            replay = MODULE.migrate(source, target)
            self.assertEqual(replay["copied"], 0)
            self.assertEqual(replay["verified"], len(MODULE.LIVE_FILES))

    def test_symlinked_allowlisted_item_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "legacy"
            source.mkdir()
            (source / "note-cookies.json").symlink_to(root / "missing")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                MODULE.migrate(source, root / "target")


if __name__ == "__main__":
    unittest.main()
