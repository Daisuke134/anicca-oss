import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import importlib.util


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/writer-agent/scripts/zenn_checkout.py"
SPEC = importlib.util.spec_from_file_location("zenn_checkout", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ZennCheckoutTest(unittest.TestCase):
    def make_remote(self, root: Path) -> Path:
        remote = root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        return remote

    def run_script(self, repo: Path, remote: str, account: str = "writer-user"):
        return subprocess.run(
            [
                "python3", str(SCRIPT), "--repo", str(repo), "--remote", remote,
                "--account", account, "--git-name", "Example Writer",
                "--git-email", "writer@example.com",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_clean_user_gets_managed_checkout_and_idempotent_replay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            remote = self.make_remote(root)
            repo = root / ".local/state/life-manager/writer/checkouts/zenn-articles"
            first = self.run_script(repo, str(remote))
            second = self.run_script(repo, str(remote))
            self.assertEqual([first.returncode, second.returncode], [0, 0], (first.stderr, second.stderr))
            self.assertEqual(json.loads(first.stdout)["repo"], str(repo.resolve()))
            self.assertTrue((repo / ".git").is_dir())

    def test_missing_remote_or_account_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "checkout"
            self.assertNotEqual(self.run_script(repo, "").returncode, 0)
            self.assertNotEqual(self.run_script(repo, str(self.make_remote(Path(temp))), "").returncode, 0)

    def test_legacy_path_and_wrong_existing_remote_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote = self.make_remote(root)
            legacy = root / ".openclaw/workspace/zenn-articles"
            self.assertNotEqual(self.run_script(legacy, str(remote)).returncode, 0)
            repo = root / "managed"
            other = self.make_remote(root / "other")
            subprocess.run(["git", "clone", str(other), str(repo)], check=True, capture_output=True)
            self.assertNotEqual(self.run_script(repo, str(remote)).returncode, 0)

    def test_symlink_checkout_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            remote = self.make_remote(root)
            real = root / "real"
            subprocess.run(["git", "clone", str(remote), str(real)], check=True, capture_output=True)
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            self.assertNotEqual(self.run_script(link, str(remote)).returncode, 0)

    def test_source_contains_no_operator_credentials(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("Daisuke134", text)
        self.assertNotIn("anicca@", text)
        self.assertNotIn("id_ed25519", text)

    def test_configured_ssh_key_is_passed_to_initial_clone(self):
        completed = subprocess.CompletedProcess(["git"], 0, "", "")
        with mock.patch.dict(os.environ, {"ZENN_SSH_KEY": "/private/key"}, clear=False):
            with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
                MODULE._git("clone", "git@example.test:writer/articles.git", "/tmp/repo")
        self.assertEqual(
            run.call_args.kwargs["env"]["GIT_SSH_COMMAND"],
            "ssh -i /private/key -o IdentitiesOnly=yes",
        )


if __name__ == "__main__":
    unittest.main()
