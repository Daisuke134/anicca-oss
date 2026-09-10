import importlib.util
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/migrate-legacy-x402-state.py"
SPEC = importlib.util.spec_from_file_location("migrate_legacy_x402_state", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def args(root: Path, mode: str = "precopy", sealed: bool = False):
    return SimpleNamespace(
        mode=mode,
        source_sealed=sealed,
        x402_source=str(root / "old-x402"),
        the402_sqlite=str(root / "old-inbox.sqlite"),
        target_root=str(root / "new-x402"),
    )


class MigrateLegacyX402StateTests(unittest.TestCase):
    def prepare(self, root: Path):
        source = root / "old-x402"
        source.mkdir(mode=0o700)
        (source / "sales-wallet.jsonl").write_text(json.dumps({"sale": 1}) + "\n")
        (source / "market-scout.json").write_text(json.dumps({"offers": 2}))
        with closing(sqlite3.connect(root / "old-inbox.sqlite")) as database:
            database.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
            database.execute("INSERT INTO jobs VALUES ('job-1')")
            database.commit()

    def test_precopy_preserves_json_and_online_sqlite_then_replay_skips(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            first = MODULE.migrate(args(root))
            self.assertEqual(first, {"mode": "precopy", "files_copied": 2, "files_skipped": 0, "sqlite_copied": 1, "sqlite_skipped": 0})
            (root / "new-x402/the402-inbox.sqlite").chmod(0o644)
            replay = MODULE.migrate(args(root))
            self.assertEqual(replay, {"mode": "precopy", "files_copied": 0, "files_skipped": 2, "sqlite_copied": 0, "sqlite_skipped": 1})
            self.assertTrue((root / "old-x402/sales-wallet.jsonl").exists())
            with closing(sqlite3.connect(root / "new-x402/the402-inbox.sqlite")) as database:
                self.assertEqual(database.execute("SELECT id FROM jobs").fetchall(), [("job-1",)])
                self.assertEqual(database.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual((root / "new-x402").stat().st_mode & 0o777, 0o700)
            for path in (root / "new-x402").iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_precopy_fails_when_live_sqlite_source_diverges_after_first_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            MODULE.migrate(args(root))
            with closing(sqlite3.connect(root / "old-inbox.sqlite")) as database:
                database.execute("INSERT INTO jobs VALUES ('job-2')")
                database.commit()
            with self.assertRaisesRegex(RuntimeError, "target differs during precopy"):
                MODULE.migrate(args(root))

    def test_precopy_fails_on_unrelated_but_integrity_valid_sqlite_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            target_root = root / "new-x402"
            target_root.mkdir()
            with closing(sqlite3.connect(target_root / "the402-inbox.sqlite")) as database:
                database.execute("CREATE TABLE other (id TEXT PRIMARY KEY)")
                database.commit()
            with self.assertRaisesRegex(RuntimeError, "target differs during precopy"):
                MODULE.migrate(args(root))

    def test_precopy_fails_closed_on_divergent_existing_business_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            target = root / "new-x402"
            target.mkdir()
            (target / "sales-wallet.jsonl").write_text("different\n")
            with self.assertRaisesRegex(RuntimeError, "target differs during precopy"):
                MODULE.migrate(args(root))

    def test_converge_requires_explicit_sealed_source_and_replaces_after_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            MODULE.migrate(args(root))
            with (root / "old-x402/sales-wallet.jsonl").open("a") as history:
                history.write('{"sale": 2}\n')
            with closing(sqlite3.connect(root / "old-inbox.sqlite")) as database:
                database.execute("INSERT INTO jobs VALUES ('job-2')")
                database.commit()
            with self.assertRaisesRegex(ValueError, "requires --source-sealed"):
                MODULE.migrate(args(root, mode="converge"))
            result = MODULE.migrate(args(root, mode="converge", sealed=True))
            self.assertEqual(result["files_copied"], 1)
            self.assertEqual((root / "new-x402/sales-wallet.jsonl").read_text(), '{"sale": 1}\n{"sale": 2}\n')
            with closing(sqlite3.connect(root / "new-x402/the402-inbox.sqlite")) as database:
                self.assertEqual(database.execute("SELECT count(*) FROM jobs").fetchone(), (2,))

    def test_converge_preserves_newer_canonical_history_snapshots_and_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            MODULE.migrate(args(root))
            target_root = root / "new-x402"
            target_history = target_root / "sales-wallet.jsonl"
            target_history.write_text('{"sale": 1}\n{"sale": 2}\n')
            target_snapshot = target_root / "market-scout.json"
            target_snapshot.write_text(json.dumps({"offers": 3}))
            with closing(sqlite3.connect(target_root / "the402-inbox.sqlite")) as database:
                database.execute("INSERT INTO jobs VALUES ('job-2')")
                database.commit()
            newer = max(path.stat().st_mtime_ns for path in target_root.iterdir()) + 1_000_000_000
            os.utime(target_snapshot, ns=(newer, newer))
            os.utime(target_root / "the402-inbox.sqlite", ns=(newer, newer))

            result = MODULE.migrate(args(root, mode="converge", sealed=True))

            self.assertEqual(result, {"mode": "converge", "files_copied": 0, "files_skipped": 2, "sqlite_copied": 0, "sqlite_skipped": 1})
            self.assertEqual(target_history.read_text(), '{"sale": 1}\n{"sale": 2}\n')
            self.assertEqual(json.loads(target_snapshot.read_text()), {"offers": 3})
            with closing(sqlite3.connect(target_root / "the402-inbox.sqlite")) as database:
                self.assertEqual(database.execute("SELECT count(*) FROM jobs").fetchone(), (2,))

    def test_converge_rejects_divergent_append_only_histories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            MODULE.migrate(args(root))
            (root / "new-x402/sales-wallet.jsonl").write_text('{"different": true}\n')

            with self.assertRaisesRegex(RuntimeError, "append-only histories diverge"):
                MODULE.migrate(args(root, mode="converge", sealed=True))

    def test_converge_rejects_divergent_sqlite_histories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            MODULE.migrate(args(root))
            with closing(sqlite3.connect(root / "old-inbox.sqlite")) as database:
                database.execute("INSERT INTO jobs VALUES ('source-only')")
                database.commit()
            with closing(sqlite3.connect(root / "new-x402/the402-inbox.sqlite")) as database:
                database.execute("INSERT INTO jobs VALUES ('target-only')")
                database.commit()

            with self.assertRaisesRegex(RuntimeError, "SQLite histories diverge"):
                MODULE.migrate(args(root, mode="converge", sealed=True))

    def test_sqlite_temporary_database_cleanup_removes_wal_and_shm(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory) / ".the402-inbox.sqlite.test"
            temporary.write_bytes(b"db")
            Path(f"{temporary}-wal").write_bytes(b"wal")
            Path(f"{temporary}-shm").write_bytes(b"shm")

            MODULE.cleanup_sqlite_artifacts(temporary)

            self.assertFalse(temporary.exists())
            self.assertFalse(Path(f"{temporary}-wal").exists())
            self.assertFalse(Path(f"{temporary}-shm").exists())

    def test_converge_refuses_to_replace_target_with_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            MODULE.migrate(args(root))
            with closing(sqlite3.connect(root / "old-inbox.sqlite")) as database:
                database.execute("INSERT INTO jobs VALUES ('job-2')")
                database.commit()
            Path(f"{root / 'new-x402/the402-inbox.sqlite'}-wal").write_bytes(b"live")

            with self.assertRaisesRegex(RuntimeError, "target SQLite sidecars present"):
                MODULE.migrate(args(root, mode="converge", sealed=True))

    def test_converge_refuses_orphan_target_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            target_root = root / "new-x402"
            target_root.mkdir()
            Path(f"{target_root / 'the402-inbox.sqlite'}-shm").write_bytes(b"orphan")

            with self.assertRaisesRegex(RuntimeError, "orphan target SQLite sidecars present"):
                MODULE.migrate(args(root, mode="converge", sealed=True))

    def test_symlinked_source_or_target_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.prepare(root)
            outside = root / "outside"
            outside.mkdir()
            (root / "new-x402").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink directory"):
                MODULE.migrate(args(root))


if __name__ == "__main__":
    unittest.main()
