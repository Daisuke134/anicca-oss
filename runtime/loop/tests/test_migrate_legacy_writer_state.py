import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/migrate-legacy-writer-state.py"
SPEC = importlib.util.spec_from_file_location("migrate_writer", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)
import legacy_state_mirror


class WriterMigrationTest(unittest.TestCase):
    def stores(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        source = root / ".openclaw"
        target = root / "writer"
        (source / "state").mkdir(parents=True)
        (source / "logs/article-writer/nested").mkdir(parents=True)
        target.mkdir()
        os.chmod(target, 0o751)
        (target / "money.sqlite3").write_text("current business state\n")
        (target / "owned-elsewhere").mkdir(mode=0o755)
        (source / "state/.article-last-run").write_text("state\n")
        (source / "state/.self-fix-writer-agent-last").write_text("repair\n")
        (source / "state/unrelated.json").write_text("ignore\n")
        (source / "logs/writer-report.log").write_text("writer\n")
        (source / "logs/article-daily.log").write_text("article\n")
        (source / "logs/self-fix-writer-agent.log").write_text("self-fix\n")
        (source / "logs/unrelated.log").write_text("ignore\n")
        (source / "logs/article-writer/nested/run.log").write_text("nested\n")
        return temporary, source, target

    def test_allowlist_copies_into_existing_writer_store(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        result = MODULE.migrate(source, target)
        self.assertEqual(result, {"copied": 6, "skipped": 0, "verified": 6, "sealed": False})
        self.assertEqual((target / "money.sqlite3").read_text(), "current business state\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o751)
        self.assertEqual((target / "owned-elsewhere").stat().st_mode & 0o777, 0o755)
        self.assertEqual((target / "logs").stat().st_mode & 0o777, 0o700)
        self.assertFalse((target / "unrelated.json").exists())
        self.assertFalse((target / "logs/unrelated.log").exists())

    def test_unrelated_existing_target_symlink_is_ignored(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        outside = target.parent / "outside-unrelated"
        outside.mkdir()
        (target / "owned-elsewhere/link").symlink_to(outside, target_is_directory=True)
        result = MODULE.migrate(source, target)
        self.assertEqual(result["verified"], 6)
        self.assertTrue((target / "owned-elsewhere/link").is_symlink())

    def test_unchanged_rerun_and_owned_update(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        MODULE.migrate(source, target)
        self.assertEqual(MODULE.migrate(source, target)["skipped"], 6)
        legacy = source / "logs/writer-report.log"
        legacy.write_text("writer updated\n")
        with self.assertRaisesRegex(ValueError, "sealed idle cutover"):
            MODULE.migrate(source, target)
        result = MODULE.migrate(source, target, seal=True)
        self.assertEqual(result["copied"], 1)
        self.assertEqual((target / "logs/writer-report.log").read_text(), legacy.read_text())

    def test_changed_destination_fails_closed(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        MODULE.migrate(source, target)
        (target / "logs/writer-report.log").write_text("new owner data\n")
        with self.assertRaisesRegex(ValueError, "outside migration"):
            MODULE.migrate(source, target)

    def test_seal_prevents_future_writes(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        self.assertTrue(MODULE.migrate(source, target, seal=True)["sealed"])
        with self.assertRaisesRegex(ValueError, "sealed"):
            MODULE.migrate(source, target)

    def test_symlink_in_selected_tree_fails_closed(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        (source / "logs/article-writer/link").symlink_to(source / "state", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            MODULE.migrate(source, target)

    def test_allowlisted_top_level_symlink_fails_closed(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        (source / "logs/writer-link.log").symlink_to(source / "logs/unrelated.log")
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            MODULE.migrate(source, target)

    def test_source_and_target_root_symlinks_fail_closed(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        source_link = source.parent / "source-link"
        source_link.symlink_to(source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink source root"):
            MODULE.migrate(source_link, target)
        target_link = target.parent / "target-link"
        target_link.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink target root"):
            MODULE.migrate(source, target_link)

    def test_target_change_after_preflight_fails_closed(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        MODULE.migrate(source, target)
        real_atomic = legacy_state_mirror.atomic_json
        calls = 0

        def mutate_after_preflight(path, value, *args):
            nonlocal calls
            real_atomic(path, value, *args)
            calls += 1
            if calls == 1:
                (target / "logs/writer-report.log").write_text("external change\n")

        (source / "logs/writer-report.log").write_text("source update\n")
        with mock.patch.object(legacy_state_mirror, "atomic_json", side_effect=mutate_after_preflight):
            with self.assertRaisesRegex(ValueError, "changed after preflight"):
                MODULE.migrate(source, target)
        self.assertEqual((target / "logs/writer-report.log").read_text(), "external change\n")

    def test_source_change_after_preflight_copies_and_verifies_latest(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        real_atomic = legacy_state_mirror.atomic_json
        calls = 0

        def mutate_after_preflight(path, value, *args):
            nonlocal calls
            real_atomic(path, value, *args)
            calls += 1
            if calls == 1:
                (source / "logs/writer-report.log").write_text("latest source\n")

        with mock.patch.object(legacy_state_mirror, "atomic_json", side_effect=mutate_after_preflight):
            result = MODULE.migrate(source, target)
        self.assertEqual(result["verified"], 6)
        self.assertEqual((target / "logs/writer-report.log").read_text(), "latest source\n")

    def test_existing_file_as_target_parent_fails_before_marker(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        (target / "logs").write_text("not a directory\n")
        with self.assertRaisesRegex(ValueError, "target parent"):
            MODULE.migrate(source, target)
        self.assertFalse((target / MODULE.MARKER).exists())

    def test_identical_existing_target_without_marker_is_not_adopted(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        (target / "logs").mkdir()
        legacy = source / "logs/writer-report.log"
        (target / "logs/writer-report.log").write_bytes(legacy.read_bytes())
        with self.assertRaisesRegex(ValueError, "no ownership marker"):
            MODULE.migrate(source, target)
        self.assertFalse((target / MODULE.MARKER).exists())

    def test_dangling_target_symlink_fails_before_marker(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        (target / "logs").mkdir()
        (target / "logs/writer-report.log").symlink_to(target / "missing")
        with self.assertRaisesRegex(ValueError, "unsafe migration target"):
            MODULE.migrate(source, target)
        self.assertFalse((target / MODULE.MARKER).exists())

    def test_source_symlink_swap_after_preflight_is_rejected(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        real_atomic = legacy_state_mirror.atomic_json
        external = source.parent / "outside.txt"
        external.write_text("outside\n")
        swapped = False

        def swap_source(path, value, *args):
            nonlocal swapped
            real_atomic(path, value, *args)
            if not swapped:
                selected = source / "logs/writer-report.log"
                selected.unlink()
                selected.symlink_to(external)
                swapped = True

        with mock.patch.object(legacy_state_mirror, "atomic_json", side_effect=swap_source):
            with self.assertRaisesRegex(ValueError, "symlink migration source"):
                MODULE.migrate(source, target)
        self.assertFalse((target / "logs/writer-report.log").exists())

    def test_target_parent_symlink_swap_after_preflight_is_rejected(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        real_atomic = legacy_state_mirror.atomic_json
        external = source.parent / "outside"
        external.mkdir()
        swapped = False

        def swap_parent(path, value, *args):
            nonlocal swapped
            real_atomic(path, value, *args)
            if not swapped:
                logs = target / "logs"
                logs.rename(target / "logs-original")
                logs.symlink_to(external, target_is_directory=True)
                swapped = True

        with mock.patch.object(legacy_state_mirror, "atomic_json", side_effect=swap_parent):
            with self.assertRaisesRegex(ValueError, "target parent"):
                MODULE.migrate(source, target)
        self.assertEqual(list(external.iterdir()), [])

    def test_source_parent_symlink_swap_after_preflight_is_rejected(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        real_atomic = legacy_state_mirror.atomic_json
        external = source.parent / "outside-logs"
        external.mkdir()
        (external / "writer-report.log").write_text("outside\n")
        swapped = False

        def swap_source_parent(path, value, *args):
            nonlocal swapped
            real_atomic(path, value, *args)
            if not swapped:
                logs = source / "logs"
                logs.rename(source / "logs-original")
                logs.symlink_to(external, target_is_directory=True)
                swapped = True

        with mock.patch.object(legacy_state_mirror, "atomic_json", side_effect=swap_source_parent):
            with self.assertRaisesRegex(ValueError, "symlink migration source"):
                MODULE.migrate(source, target)
        self.assertFalse((target / "logs/writer-report.log").exists())

    def test_target_root_symlink_swap_before_final_marker_is_rejected(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        external = source.parent / "outside-target"
        external.mkdir()
        real_copy = legacy_state_mirror.stable_copy
        calls = 0

        def swap_target_root(
            source_path, source_root, target_root, target_path, expected_target_digest
        ):
            nonlocal calls
            result = real_copy(
                source_path, source_root, target_root, target_path, expected_target_digest
            )
            calls += 1
            if calls == 6:
                target.rename(target.parent / "writer-original")
                target.symlink_to(external, target_is_directory=True)
            return result

        with mock.patch.object(legacy_state_mirror, "stable_copy", side_effect=swap_target_root):
            with self.assertRaisesRegex(ValueError, "target parent"):
                MODULE.migrate(source, target)
        self.assertFalse((external / MODULE.MARKER).exists())

    def test_dangling_writer_logs_root_is_rejected(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        logs = source / "logs"
        for path in sorted(logs.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        logs.rmdir()
        logs.symlink_to(source / "missing", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink legacy Writer logs"):
            MODULE.migrate(source, target)
        self.assertFalse((target / MODULE.MARKER).exists())

    def test_atomic_marker_rechecks_target_root_internally(self):
        temporary, source, target = self.stores()
        self.addCleanup(temporary.cleanup)
        external = source.parent / "outside-marker"
        external.mkdir()
        real_atomic = legacy_state_mirror.atomic_json
        calls = 0

        def swap_before_real_atomic(path, value, *args):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.rename(target.parent / "writer-before-final-marker")
                target.symlink_to(external, target_is_directory=True)
            return real_atomic(path, value, *args)

        with mock.patch.object(
            legacy_state_mirror, "atomic_json", side_effect=swap_before_real_atomic
        ):
            with self.assertRaisesRegex(ValueError, "target parent"):
                MODULE.migrate(source, target)
        self.assertFalse((external / MODULE.MARKER).exists())


if __name__ == "__main__":
    unittest.main()
