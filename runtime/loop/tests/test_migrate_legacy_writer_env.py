import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/migrate-legacy-writer-env.py"
SPEC = importlib.util.spec_from_file_location("migrate_writer_env", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class WriterEnvironmentMigrationTest(unittest.TestCase):
    def test_only_allowlisted_missing_keys_are_copied_without_changing_existing_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "legacy.env"
            target = root / "life-manager.env"
            source.write_text(
                "DEVTO_API_KEY='secret value'\nUNRELATED_SECRET=never-copy\n"
                "export NOTE_EMAIL=writer@example.test\n"
            )
            target.write_text("KEEP=this-line\n")
            result = MODULE.migrate(source, target)
            self.assertEqual(result, {"copied": 2, "skipped": 0, "missing": 6})
            body = target.read_text()
            self.assertTrue(body.startswith("KEEP=this-line\n"))
            self.assertIn("DEVTO_API_KEY='secret value'\n", body)
            self.assertIn("NOTE_EMAIL=writer@example.test\n", body)
            self.assertNotIn("UNRELATED_SECRET", body)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
            self.assertEqual(MODULE.migrate(source, target)["copied"], 0)

    def test_different_existing_value_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "legacy.env"
            target = root / "life-manager.env"
            source.write_text("DEVTO_API_KEY=legacy\n")
            target.write_text("DEVTO_API_KEY=current\n")
            with self.assertRaisesRegex(ValueError, "different"):
                MODULE.migrate(source, target)
            target.unlink()
            target.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                MODULE.migrate(source, target)

    def test_duplicate_allowlisted_source_key_fails_without_target_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "legacy.env"
            target = root / "life-manager.env"
            source.write_text("NOTE_PASSWORD=one\nexport NOTE_PASSWORD=two\n")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                MODULE.migrate(source, target)
            self.assertFalse(target.exists())

    def test_symlink_parent_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "legacy.env"
            source.write_text("DEVTO_API_KEY=legacy\n")
            real = root / "real"
            (real / "child").mkdir(parents=True)
            linked = root / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink path component"):
                MODULE.migrate(source, linked / "child/life-manager.env")


if __name__ == "__main__":
    unittest.main()
