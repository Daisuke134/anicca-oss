import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from runtime.host.browser_port_owner import _process_group_exists, _terminate_process_group
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "browser_port_owner.py"
LANCERS_LAUNCHER = Path(__file__).parents[3] / "skills/earn/lancers/scripts/browser-owner"


class BrowserPortOwnerTests(unittest.TestCase):
    def test_permission_denied_probe_means_group_still_exists(self):
        with patch("runtime.host.browser_port_owner.os.killpg", side_effect=PermissionError):
            self.assertTrue(_process_group_exists(43210))

    def test_cleanup_fails_closed_if_owned_group_survives_sigkill(self):
        with (
            patch("runtime.host.browser_port_owner._process_group_exists", return_value=True),
            patch("runtime.host.browser_port_owner.os.killpg"),
            patch("runtime.host.browser_port_owner.time.sleep"),
            patch("runtime.host.browser_port_owner.time.monotonic", side_effect=[0, 2, 2, 4]),
            pytest.raises(RuntimeError, match="survived SIGKILL"),
        ):
            _terminate_process_group(43210, grace_seconds=1)

    def test_process_group_is_terminated_before_owner_releases_lease(self):
        args = type("Args", (), {
            "state_dir": Path("/tmp/browser-owner-test-state"),
            "profile": "/profiles/owned",
            "port": 9224,
            "owner": "owned",
            "command": ["--", "/usr/bin/true"],
        })()
        child = MagicMock(pid=43210)
        child.wait.return_value = 0
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(args, "state_dir", Path(temporary) / "state"),
            patch("runtime.host.browser_port_owner.subprocess.Popen", return_value=child) as popen,
            patch("runtime.host.browser_port_owner._terminate_process_group") as terminate,
        ):
            from runtime.host.browser_port_owner import run
            self.assertEqual(run(args), 0)
        popen.assert_called_once_with(["/usr/bin/true"], start_new_session=True)
        terminate.assert_called_once_with(43210)

    def test_descendant_cannot_survive_after_browser_root_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            root_pid_path = root / "root-pid"
            child_code = (
                "import pathlib,subprocess,sys;"
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
                f"pathlib.Path({str(root_pid_path)!r}).write_text(str(__import__('os').getpid()))"
            )
            owner = subprocess.Popen([
                sys.executable, str(SCRIPT), "run", "--state-dir", str(state),
                "--port", "9225", "--profile", "/profiles/descendant",
                "--owner", "descendant", "--", sys.executable, "-c", child_code,
            ])
            self.assertEqual(owner.wait(timeout=5), 0)
            pgid = int(root_pid_path.read_text())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("browser descendant process group survived lease release")
            self.assertFalse((state / "9225.json").exists())

    def test_lancers_launcher_reexecutes_through_shared_owner(self):
        launcher = LANCERS_LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('runtime/host/browser_port_owner.py', launcher)
        self.assertIn('--owner lancers-revenue-browser', launcher)
        self.assertIn('LANCERS_BROWSER_PORT_OWNED=1', launcher)

    def test_second_owner_for_same_port_fails_closed_while_first_is_alive(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            first = subprocess.Popen(
                [sys.executable, str(SCRIPT), "run", "--state-dir", str(state),
                 "--port", "9222", "--profile", "/profiles/daily", "--owner", "daily",
                 "--", sys.executable, "-c", "import time; time.sleep(10)"],
            )
            try:
                deadline = time.monotonic() + 3
                receipt = state / "9222.json"
                while not receipt.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(receipt.exists())
                second = subprocess.run(
                    [sys.executable, str(SCRIPT), "run", "--state-dir", str(state),
                     "--port", "9222", "--profile", "/profiles/job-search", "--owner", "job-search",
                     "--", sys.executable, "-c", "raise SystemExit(0)"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(second.returncode, 75)
                conflict = json.loads(second.stderr)
                self.assertEqual(conflict["reason"], "browser_port_owned")
                self.assertEqual(conflict["port"], 9222)
                self.assertEqual(conflict["current_owner"], "daily")
                self.assertNotIn("profile", conflict)
            finally:
                first.terminate()
                first.wait(timeout=3)

    def test_different_ports_can_run_concurrently(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            first = subprocess.Popen(
                [sys.executable, str(SCRIPT), "run", "--state-dir", str(state),
                 "--port", "9222", "--profile", "/profiles/daily", "--owner", "daily",
                 "--", sys.executable, "-c", "import time; time.sleep(10)"],
            )
            try:
                time.sleep(0.1)
                second = subprocess.run(
                    [sys.executable, str(SCRIPT), "run", "--state-dir", str(state),
                     "--port", "9223", "--profile", "/profiles/gig", "--owner", "gig",
                     "--", sys.executable, "-c", "raise SystemExit(0)"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(second.returncode, 0, second.stderr)
            finally:
                first.terminate()
                first.wait(timeout=3)

    def test_same_profile_on_different_port_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            first = subprocess.Popen(
                [sys.executable, str(SCRIPT), "run", "--state-dir", str(state),
                 "--port", "9222", "--profile", "/profiles/shared", "--owner", "first",
                 "--", sys.executable, "-c", "import time; time.sleep(10)"],
            )
            try:
                deadline = time.monotonic() + 3
                receipt = state / "9222.json"
                while not receipt.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(receipt.exists())
                second = subprocess.run(
                    [sys.executable, str(SCRIPT), "run", "--state-dir", str(state),
                     "--port", "9223", "--profile", "/profiles/shared", "--owner", "second",
                     "--", sys.executable, "-c", "raise SystemExit(0)"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(second.returncode, 75)
                conflict = json.loads(second.stderr)
                self.assertEqual(conflict["reason"], "browser_profile_owned")
                self.assertEqual(conflict["current_owner"], "first")
                self.assertNotIn("profile", conflict)
            finally:
                first.terminate()
                first.wait(timeout=3)

    def test_receipt_attributes_supervisor_and_browser_root_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            process = subprocess.Popen(
                [sys.executable, str(SCRIPT), "run", "--state-dir", str(state),
                 "--port", "9224", "--profile", "/profiles/owned", "--owner", "owned",
                 "--", sys.executable, "-c", "import time; time.sleep(10)"],
            )
            try:
                deadline = time.monotonic() + 3
                receipt_path = state / "9224.json"
                while not receipt_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                self.assertEqual(receipt["supervisor_pid"], process.pid)
                self.assertGreater(receipt["browser_root_pid"], 0)
                self.assertNotEqual(receipt["browser_root_pid"], process.pid)
            finally:
                process.terminate()
                process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
