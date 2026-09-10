import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("warmup_migration", ROOT / "runtime/migrate-legacy-warmup-flip-state.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class WarmupMigrationTest(unittest.TestCase):
    def test_copy_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            (source / "state").mkdir(parents=True)
            (source / "logs").mkdir()
            (source / "state/postiz-integrations.json").write_text('{"integrations":[]}\n')
            (source / "logs/warmup-flip-launchd.out.log").write_text("old\n")
            (source / "logs/warmup-flip-launchd.err.log").write_text("err\n")
            self.assertEqual(MODULE.migrate(source, target), {"copied_files": 3})
            self.assertEqual(MODULE.migrate(source, target), {"copied_files": 0})
            self.assertEqual((target / "state/postiz-integrations.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual((target / "logs/warmup-flip-launchd.err.log").read_text(), "err\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_rejects_intermediate_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, outside = root / "source", root / "outside"
            source.mkdir()
            outside.mkdir()
            (source / "state").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(source, root / "target")

    def test_rejects_root_symlink_overlap_invalid_schema_and_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            (source / "state").mkdir(parents=True)
            (source / "logs").mkdir()
            state = source / "state/postiz-integrations.json"
            state.write_text('{"integrations":[]}\n')
            alias = root / "source-alias"
            alias.symlink_to(source, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(alias, target)
            with self.assertRaisesRegex(ValueError, "overlap"):
                MODULE.migrate(source, source / "child")
            state.write_text('{"integrations":{}}\n')
            with self.assertRaisesRegex(ValueError, "invalid"):
                MODULE.migrate(source, target)
            state.write_text('{"integrations":[]}\n')
            MODULE.migrate(source, target)
            (target / "state/postiz-integrations.json").write_text('{"integrations":[1]}\n')
            with self.assertRaisesRegex(ValueError, "conflicting"):
                MODULE.migrate(source, target)

    def test_rejects_target_file_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, outside = root / "source", root / "target", root / "outside"
            (source / "state").mkdir(parents=True)
            (source / "logs").mkdir()
            (source / "state/postiz-integrations.json").write_text('{"integrations":[]}\n')
            (target / "state").mkdir(parents=True)
            outside.write_text("outside")
            (target / "state/postiz-integrations.json").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "conflicting"):
                MODULE.migrate(source, target)

    def test_rejects_dangling_target_and_intermediate_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "state").mkdir(parents=True)
            (source / "logs").mkdir()
            (source / "state/postiz-integrations.json").write_text('{"integrations":[]}\n')
            dangling = root / "dangling"
            dangling.symlink_to(root / "missing", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(source, dangling)
            outside = root / "outside"
            outside.mkdir()
            middle = root / "middle"
            middle.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(source, middle / "target")

    def test_rejects_non_object_integration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "state").mkdir(parents=True)
            (source / "logs").mkdir()
            (source / "state/postiz-integrations.json").write_text('{"integrations":[1]}\n')
            with self.assertRaisesRegex(ValueError, "invalid"):
                MODULE.migrate(source, root / "target")

    def test_rejects_dangling_target_file_and_optional_log_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            (source / "state").mkdir(parents=True)
            (source / "logs").mkdir()
            (source / "state/postiz-integrations.json").write_text('{"integrations":[]}\n')
            (target / "state").mkdir(parents=True)
            (target / "state/postiz-integrations.json").symlink_to(root / "missing-state")
            with self.assertRaisesRegex(ValueError, "conflicting"):
                MODULE.migrate(source, target)
            (target / "state/postiz-integrations.json").unlink()
            (source / "logs/warmup-flip-launchd.out.log").symlink_to(root / "missing-log")
            with self.assertRaisesRegex(ValueError, "invalid"):
                MODULE.migrate(source, target)


if __name__ == "__main__":
    unittest.main()
