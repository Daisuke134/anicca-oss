import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "runtime/configure-zenn.py"
SPEC = importlib.util.spec_from_file_location("configure_zenn", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ConfigureZennTest(unittest.TestCase):
    def values(self):
        return {
            "ZENN_ACCOUNT": "example-writer",
            "ZENN_REPOSITORY_URL": "https://github.com/example/zenn-content.git",
            "ZENN_GIT_NAME": "Example Writer",
            "ZENN_GIT_EMAIL": "writer@example.com",
            "ARTICLE_MEDIA_RAW_BASE": "https://raw.githubusercontent.com/example/zenn-content/main/images",
        }

    def test_append_only_configuration_is_private_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp).resolve() / "state/.env"
            target.parent.mkdir()
            target.write_text("EXISTING=value\n", encoding="utf-8")
            self.assertEqual(MODULE.configure(target, self.values()), {"configured": 5, "skipped": 0})
            self.assertEqual(MODULE.configure(target, self.values()), {"configured": 0, "skipped": 5})
            text = target.read_text(encoding="utf-8")
            self.assertIn("EXISTING=value\n", text)
            self.assertIn("ZENN_ACCOUNT=example-writer\n", text)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            loaded = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -e; source "$1"; printf "%s|%s" "$ZENN_GIT_NAME" "$ZENN_GIT_EMAIL"',
                    "bash",
                    str(target),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            self.assertEqual(loaded.stdout, "Example Writer|writer@example.com")

    def test_conflict_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / ".env"
            target.write_text("ZENN_ACCOUNT=someone-else\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicting"):
                MODULE.configure(target, self.values())
            real = root / "real.env"
            real.write_text("", encoding="utf-8")
            link = root / "link.env"
            link.symlink_to(real)
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.configure(link, self.values())

    def test_shell_metacharacters_round_trip_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / ".env"
            marker = root / "executed"
            values = self.values()
            values["ZENN_GIT_NAME"] = f"$(touch {marker})"
            MODULE.configure(target, values)
            loaded = subprocess.run(
                ["bash", "-c", 'set -e; source "$1"; printf "%s" "$ZENN_GIT_NAME"', "bash", str(target)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            self.assertEqual(loaded.stdout, values["ZENN_GIT_NAME"])
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
