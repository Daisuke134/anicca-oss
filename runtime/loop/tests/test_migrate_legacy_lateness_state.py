import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("lateness_migration", ROOT / "runtime/migrate-legacy-lateness-state.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class LatenessMigrationTest(unittest.TestCase):
    def fixture(self, root: Path):
        source = root / "legacy"
        (source / "identity").mkdir(parents=True)
        (source / "state/location").mkdir(parents=True)
        (source / "skills/anicca-life-manager/state").mkdir(parents=True)
        (source / "identity/profile.json").write_text('{"name":"user"}\n')
        (source / "state/location/123.json").write_text('{"lat":1}\n')
        (source / "state/call-bridge").mkdir(parents=True)
        (source / "state/call-bridge/public_url.txt").write_text("https://example.test\n")
        (source / "state/arrival").mkdir(parents=True)
        (source / "state/arrival/notified.json").write_text("{}\n")
        (source / "skills/anicca-life-manager/state/run.log").write_text("old\n")
        (source / "skills/anicca-life-manager/state/heartbeat_log.jsonl").write_text('{"ok":true}\n')
        (source / "skills/anicca-life-manager/state/nudge_sent.json").write_text("[]\n")
        (source / "skills/anicca-life-manager/state/active_call_loop.json").write_text("{}\n")
        (source / "skills/anicca-life-manager/state/renraku_sent.json").write_text("[]\n")
        return source, root / "canonical", root / "loop"

    def test_copy_private_and_replay_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            source, home, loop = self.fixture(Path(directory))
            self.assertEqual(MODULE.migrate(source, home, loop), {"copied_files": 9})
            self.assertEqual(MODULE.migrate(source, home, loop), {"copied_files": 0})
            for path in (
                home / "identity/profile.json",
                home / "state/location/123.json",
                home / "state/call-bridge/public_url.txt",
                home / "state/arrival/notified.json",
                loop / "logs/run.log",
                loop / "state/heartbeat_log.jsonl",
                loop / "state/nudge_sent.json",
                loop / "state/active_call_loop.json",
                loop / "state/renraku_sent.json",
                loop / "legacy-migration.json",
            ):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_clean_install_without_legacy_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop = root / "loop"
            self.assertEqual(
                MODULE.migrate(root / "missing-legacy", root / "canonical", loop),
                {"copied_files": 0},
            )
            self.assertTrue((loop / "legacy-migration.json").is_file())

    def test_rejects_conflict_symlink_and_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, home, loop = self.fixture(root)
            (home / "identity").mkdir(parents=True)
            (home / "identity/profile.json").write_text("different")
            with self.assertRaisesRegex(ValueError, "conflicting"):
                MODULE.migrate(source, home, loop)
            with self.assertRaisesRegex(ValueError, "overlap"):
                MODULE.migrate(source, source / "target", loop)
            with self.assertRaisesRegex(ValueError, "cannot contain"):
                MODULE.migrate(source, root / "target" / ".." / "home", loop)
            (home / "identity/profile.json").unlink()
            (home / "identity/profile.json").symlink_to(root / "missing")
            with self.assertRaisesRegex(ValueError, "conflicting"):
                MODULE.migrate(source, home, loop)

    def test_rejects_intermediate_and_optional_dangling_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, loop = self.fixture(root)
            outside = root / "outside"
            outside.mkdir()
            middle = root / "middle"
            middle.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.migrate(source, middle / "home", loop)
            optional = source / "state/arrival/notified.json"
            optional.parent.mkdir(parents=True, exist_ok=True)
            optional.unlink()
            optional.symlink_to(root / "missing")
            with self.assertRaisesRegex(ValueError, "invalid"):
                MODULE.migrate(source, root / "home", loop)
