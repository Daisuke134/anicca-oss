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
            self.assertEqual(
                result,
                {"copied": 2, "skipped": 0, "missing": len(MODULE.KEYS) - 2},
            )
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

    def test_configure_adds_only_allowlisted_values_without_exposing_or_overwriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "life-manager.env"
            result = MODULE.configure(
                target,
                ["NOTE_URLNAME=writer name", "ARTICLE_PRODUCT_LANDING_URL=https://example.test/p"],
            )
            self.assertEqual(result, {"configured": 2, "skipped": 0})
            body = target.read_text(encoding="utf-8")
            self.assertIn("NOTE_URLNAME='writer name'", body)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)
            self.assertEqual(
                MODULE.configure(target, ["NOTE_URLNAME=writer name"]),
                {"configured": 0, "skipped": 1},
            )
            with self.assertRaisesRegex(ValueError, "different"):
                MODULE.configure(target, ["NOTE_URLNAME=another"])
            with self.assertRaisesRegex(ValueError, "allowlisted"):
                MODULE.configure(target, ["UNRELATED_SECRET=value"])

    def test_configure_rejects_non_regular_target_before_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            fifo = root / "environment.fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                MODULE.configure(fifo, ["NOTE_URLNAME=writer"])
            with self.assertRaisesRegex(ValueError, "unsafe"):
                MODULE.configure(root, ["NOTE_URLNAME=writer"])

    def test_configure_tightens_existing_permissions_before_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "life-manager.env"
            target.write_text("KEEP=value\n", encoding="utf-8")
            target.chmod(0o644)
            MODULE.configure(target, ["NOTE_URLNAME=writer"])
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o600)

    def test_configure_accepts_explicit_cliproxy_config_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "life-manager.env"
            result = MODULE.configure(
                target, ["ARTICLE_CLIPROXY_CONFIG=/etc/life-manager/cliproxy.conf"]
            )
            self.assertEqual(result, {"configured": 1, "skipped": 0})
            self.assertIn(
                "ARTICLE_CLIPROXY_CONFIG=/etc/life-manager/cliproxy.conf\n",
                target.read_text(encoding="utf-8"),
            )

    def test_configure_accepts_explicit_reinvest_wallet_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "life-manager.env"
            result = MODULE.configure(
                target, ["REINVEST_ANICCA_HOME=/srv/life-manager/instances/founder"]
            )
            self.assertEqual(result, {"configured": 1, "skipped": 0})
            self.assertIn(
                "REINVEST_ANICCA_HOME=/srv/life-manager/instances/founder\n",
                target.read_text(encoding="utf-8"),
            )

    def test_configure_accepts_earn_watch_install_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary).resolve() / "life-manager.env"
            result = MODULE.configure(target, [
                "LIFE_MANAGER_WALLET_HOME=/srv/life-manager/instances/founder",
                "PM_DEPOSIT_WALLET=0x" + "a" * 40,
                "EARN_WATCH_PAYEE=0x" + "b" * 40,
            ])
            self.assertEqual(result, {"configured": 3, "skipped": 0})
            body = target.read_text(encoding="utf-8")
            self.assertIn("LIFE_MANAGER_WALLET_HOME=/srv/life-manager/instances/founder\n", body)
            self.assertIn("PM_DEPOSIT_WALLET=0x" + "a" * 40 + "\n", body)
            self.assertIn("EARN_WATCH_PAYEE=0x" + "b" * 40 + "\n", body)


if __name__ == "__main__":
    unittest.main()
