import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/migrate-legacy-x-social-state.py"
SPEC = importlib.util.spec_from_file_location("migrate_x_social", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)
import legacy_state_mirror


class XSocialMigrationTest(unittest.TestCase):
    def stores(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        source = root / "legacy"
        target = root / "life-manager"
        for name in MODULE.MAPPINGS:
            (source / name / "evidence").mkdir(parents=True)
            (source / name / "ledger.jsonl").write_text('{"id":1}\n')
            (source / name / "evidence" / "receipt.json").write_text('{"ok":true}\n')
        return temp, source, target

    def test_first_copy_and_unchanged_rerun_are_verified(self):
        temp, source, target = self.stores()
        self.addCleanup(temp.cleanup)
        first = MODULE.migrate(source, target)
        self.assertEqual(first, {"copied": 6, "skipped": 0, "verified": 6, "sealed": False})
        second = MODULE.migrate(source, target)
        self.assertEqual(second, {"copied": 0, "skipped": 6, "verified": 6, "sealed": False})
        self.assertTrue((source / "x-repost-en" / "ledger.jsonl").exists())

    def test_rerun_updates_only_its_own_previous_copy(self):
        temp, source, target = self.stores()
        self.addCleanup(temp.cleanup)
        MODULE.migrate(source, target)
        legacy = source / "x-repost-en" / "ledger.jsonl"
        legacy.write_text('{"id":1}\n{"id":2}\n')
        with self.assertRaisesRegex(ValueError, "sealed idle cutover"):
            MODULE.migrate(source, target)
        result = MODULE.migrate(source, target, seal=True)
        self.assertEqual(result["copied"], 1)
        self.assertEqual((target / "x-repost/en/ledger.jsonl").read_text(), legacy.read_text())

    def test_changed_destination_and_unowned_destination_fail_closed(self):
        temp, source, target = self.stores()
        self.addCleanup(temp.cleanup)
        MODULE.migrate(source, target)
        (target / "x-repost/en/ledger.jsonl").write_text("new owner data\n")
        with self.assertRaisesRegex(ValueError, "outside migration"):
            MODULE.migrate(source, target)
        other = target.parent / "unowned"
        other.mkdir()
        (other / "data").write_text("keep")
        with self.assertRaisesRegex(ValueError, "no migration marker"):
            MODULE.migrate(source, other)

    def test_seal_prevents_future_migration_writes(self):
        temp, source, target = self.stores()
        self.addCleanup(temp.cleanup)
        result = MODULE.migrate(source, target, seal=True)
        self.assertTrue(result["sealed"])
        marker = json.loads((target / MODULE.MARKER).read_text())
        self.assertTrue(marker["sealed"])
        with self.assertRaisesRegex(ValueError, "sealed"):
            MODULE.migrate(source, target)

    def test_overlap_and_symlinks_are_rejected(self):
        temp, source, target = self.stores()
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(ValueError, "overlap"):
            MODULE.migrate(source, source / "nested")
        link = source / "x-repost-en" / "link"
        link.symlink_to(source / "x-repost-ja", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            MODULE.migrate(source, target)

    def test_symlink_outside_the_three_allowlisted_sources_is_ignored(self):
        temp, source, target = self.stores()
        self.addCleanup(temp.cleanup)
        (source / "current").symlink_to(source / "x-repost-en", target_is_directory=True)
        result = MODULE.migrate(source, target)
        self.assertEqual(result["verified"], 6)

    def test_first_copy_resumes_after_interruption(self):
        temp, source, target = self.stores()
        self.addCleanup(temp.cleanup)
        real_copy = legacy_state_mirror.stable_copy
        calls = 0

        def interrupted_copy(
            source_path, source_root, target_root, target_path, expected_target_digest
        ):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interruption")
            return real_copy(
                source_path, source_root, target_root, target_path, expected_target_digest
            )

        with mock.patch.object(legacy_state_mirror, "stable_copy", side_effect=interrupted_copy):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                MODULE.migrate(source, target)
        self.assertTrue((target / MODULE.MARKER).exists())
        result = MODULE.migrate(source, target)
        self.assertEqual(result["verified"], 6)
        self.assertEqual(result["copied"], 5)
        self.assertEqual(result["skipped"], 1)

    def test_dangling_allowlisted_source_is_rejected(self):
        temp, source, target = self.stores()
        self.addCleanup(temp.cleanup)
        doomed = source / "x-repost-en"
        for path in sorted(doomed.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        doomed.rmdir()
        doomed.symlink_to(source / "missing", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink legacy source"):
            MODULE.migrate(source, target)
        self.assertFalse((target / MODULE.MARKER).exists())
