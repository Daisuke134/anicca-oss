import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import pytest

from runtime.host.browser_port_owner import (
    _process_group_exists,
    _terminate_process_group,
    _wait_for_browser,
)
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "browser_port_owner.py"
LANCERS_LAUNCHER = Path(__file__).parents[3] / "skills/earn/lancers/scripts/browser-owner"


class BrowserPortOwnerTests(unittest.TestCase):
    def test_live_child_with_dead_cdp_exits_for_launchd_recovery(self):
        child = MagicMock(pid=43210)
        child.wait.side_effect = [
            subprocess.TimeoutExpired("browser", 0),
            subprocess.TimeoutExpired("browser", 0),
            subprocess.TimeoutExpired("browser", 0),
        ]
        with (
            patch("runtime.host.browser_port_owner._port_answers", return_value=False),
            patch("runtime.host.browser_port_owner.time.monotonic", side_effect=[0, 61, 62, 63]),
        ):
            self.assertEqual(
                _wait_for_browser(
                    child,
                    port=9227,
                    startup_grace_seconds=60,
                    probe_interval_seconds=0,
                    max_consecutive_failures=3,
                ),
                75,
            )

    def test_one_failed_cdp_probe_does_not_restart_a_healthy_owner(self):
        child = MagicMock(pid=43210)
        child.wait.side_effect = [
            subprocess.TimeoutExpired("browser", 0),
            subprocess.TimeoutExpired("browser", 0),
            subprocess.TimeoutExpired("browser", 0),
            0,
        ]
        with (
            patch("runtime.host.browser_port_owner._port_answers", side_effect=[True, False, True]),
            patch("runtime.host.browser_port_owner.time.monotonic", return_value=100),
        ):
            self.assertEqual(
                _wait_for_browser(
                    child,
                    port=9227,
                    startup_grace_seconds=0,
                    probe_interval_seconds=0,
                    max_consecutive_failures=3,
                ),
                0,
            )

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


# --- a supervisor that serves nothing is not an owner, 2026-09-07 ----------------------------

def _owner_module():
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "browser_port_owner.py"
    spec = importlib.util.spec_from_file_location("browser_port_owner_reclaim", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _receipt(tmp_path, **overrides):
    import json
    payload = {"owner": "lancers-revenue-browser", "port": 9227,
               "supervisor_pid": 4242, "browser_root_pid": 4243}
    payload.update(overrides)
    path = tmp_path / "9227.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_live_port_is_never_reclaimed(tmp_path, monkeypatch):
    """A healthy owner holding the lock is the lock working, not a wedge."""
    module = _owner_module()
    monkeypatch.setattr(module, "_port_answers", lambda port, timeout=3.0: True)
    monkeypatch.setattr(module, "_terminate_process_group",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not kill")))
    assert module._reclaim_wedged_owner(_receipt(tmp_path), owner="lancers-revenue-browser", port=9227) is False


def test_our_own_wedged_supervisor_is_taken_down(tmp_path, monkeypatch):
    """Measured: a Chromium up for two and a half hours that never bound its debugging port kept
    its supervisor alive, so every relaunch returned EX_TEMPFAIL and the lane applied to nothing
    until a human killed it."""
    module = _owner_module()
    killed = []
    monkeypatch.setattr(module, "_port_answers", lambda port, timeout=3.0: False)
    monkeypatch.setattr(module, "_terminate_process_group", lambda pgid, **k: killed.append(pgid))
    assert module._reclaim_wedged_owner(_receipt(tmp_path), owner="lancers-revenue-browser", port=9227) is True
    assert sorted(killed) == [4242, 4243]


def test_another_lane_s_browser_is_left_alone(tmp_path, monkeypatch):
    """Killing someone else's browser to start ours is not recovery."""
    module = _owner_module()
    monkeypatch.setattr(module, "_port_answers", lambda port, timeout=3.0: False)
    monkeypatch.setattr(module, "_terminate_process_group",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not kill")))
    receipt = _receipt(tmp_path, owner="crowdworks-revenue-browser")
    assert module._reclaim_wedged_owner(receipt, owner="lancers-revenue-browser", port=9227) is False


def test_a_receipt_for_a_different_port_is_not_acted_on(tmp_path, monkeypatch):
    module = _owner_module()
    monkeypatch.setattr(module, "_port_answers", lambda port, timeout=3.0: False)
    monkeypatch.setattr(module, "_terminate_process_group",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not kill")))
    assert module._reclaim_wedged_owner(_receipt(tmp_path, port=9228), owner="lancers-revenue-browser", port=9227) is False


def test_an_unreadable_or_pidless_receipt_reclaims_nothing(tmp_path, monkeypatch):
    module = _owner_module()
    monkeypatch.setattr(module, "_port_answers", lambda port, timeout=3.0: False)
    monkeypatch.setattr(module, "_terminate_process_group", lambda *a, **k: None)
    missing = tmp_path / "absent.json"
    assert module._reclaim_wedged_owner(missing, owner="lancers-revenue-browser", port=9227) is False
    for bad in (0, 1, True, "4242", None):
        receipt = _receipt(tmp_path, supervisor_pid=bad, browser_root_pid=bad)
        assert module._reclaim_wedged_owner(receipt, owner="lancers-revenue-browser", port=9227) is False


def test_both_lock_branches_try_to_reclaim_before_giving_up():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "browser_port_owner.py").read_text(encoding="utf-8")
    assert source.count("_reclaim_wedged_owner(") == 3  # definition + profile branch + port branch
