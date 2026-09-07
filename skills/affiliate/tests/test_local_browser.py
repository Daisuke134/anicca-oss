import builtins
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "local_browser.py"
SPEC = importlib.util.spec_from_file_location("affiliate_local_browser", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

GUARD_RELATIVE = Path(
    "gig/releases/life-manager/current/runtime/host/disk_admission.py"
)
STUB = """\
import json
import os
import sys
from pathlib import Path

capture = Path(os.environ["STUB_CAPTURE"])
keys = (
    "HOME", "LIFE_MANAGER_DISK_HEADROOM_KIB", "LIFE_MANAGER_HOST_STATE_DIR",
    "LIFE_MANAGER_PRODUCER_STATE_DIR", "LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK",
    "LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP", "GIG_DISK_HEADROOM_KIB",
    "GIG_HOST_STATE_DIR", "GIG_STATE_DIR", "GIG_IGNORE_DISK_PRESSURE_BLOCK",
    "GIG_IGNORE_DISK_WRITERS_STOP",
    "DISK_CONTROL_STATE_DIR", "OPENCLAW_STATE_DIR", "LIFE_MANAGER_HOST_STATE_DIR",
)
host_state = Path(os.environ["LIFE_MANAGER_HOST_STATE_DIR"])
reason = None
for filename, candidate in (("disk-writers.stop", "disk_writers_stop"),
                            ("disk-pressure.block", "disk_pressure_block")):
    if (host_state / filename).is_file():
        reason = candidate
        break
record = {"argv": sys.argv, "isolated": sys.flags.isolated,
          "env": {key: os.environ[key] for key in keys if key in os.environ}}
capture.write_text(json.dumps(record), encoding="utf-8")
if reason:
    receipt = Path(os.environ["LIFE_MANAGER_PRODUCER_STATE_DIR"]) / "state/disk-headroom.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"status": "failed", "failed": 1, "effect": 0,
                                   "readback": 0, "reason": reason,
                                   "required_bytes": int(os.environ["LIFE_MANAGER_DISK_HEADROOM_KIB"]) * 1024}),
                       encoding="utf-8")
raise SystemExit(1 if reason or os.environ.get("STUB_RESULT") == "1" else 0)
"""


class LocalBrowserPreflightTest(unittest.TestCase):
    def test_shared_browser_owner_environment_selects_impact_runtime(self) -> None:
        with patch.dict(os.environ, {
            "LIFE_MANAGER_LOOP_ID": "affiliate-impact-browser",
            "LIFE_MANAGER_BROWSER_CDP_PORT": "9327",
            "LIFE_MANAGER_BROWSER_PROFILE": "/tmp/affiliate-impact-profile",
        }, clear=True):
            self.assertEqual(MODULE._cdp_port(), 9327)
            self.assertEqual(MODULE._browser_profile(), Path("/tmp/affiliate-impact-profile"))
            self.assertEqual(MODULE._start_url(), "https://app.impact.com/login.user")

    def test_browser_has_a_finite_renderer_process_limit(self) -> None:
        self.assertIn(
            'f"--renderer-process-limit={renderer_limit}"',
            SCRIPT.read_text(encoding="utf-8"),
        )

    def test_browser_disables_code_sign_clone(self) -> None:
        self.assertIn(
            '"--disable-features=MacAppCodeSignClone"',
            SCRIPT.read_text(encoding="utf-8"),
        )

    def test_browser_reexecutes_through_shared_port_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "impact-en"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(MODULE.os, "execve", side_effect=SystemExit) as execute,
            ):
                with self.assertRaises(SystemExit):
                    MODULE._exec_with_owner(9327, profile, "affiliate-impact-browser")
            executable, argv, environment = execute.call_args.args
            owner_script = SCRIPT.parents[3] / "runtime/host/browser_port_owner.py"
            self.assertEqual(executable, "/usr/bin/python3")
            self.assertEqual(argv, [
                "/usr/bin/python3", "-I", str(owner_script), "run",
                "--port", "9327", "--profile", str(profile),
                "--owner", "affiliate-impact-browser", "--",
                sys.executable, str(SCRIPT),
            ])
            self.assertEqual(environment["AFFILIATE_BROWSER_PORT_OWNED"], "1")

    def install_guard(self, home: Path) -> Path:
        guard = home / GUARD_RELATIVE
        guard.parent.mkdir(parents=True)
        guard.write_text(STUB, encoding="utf-8")
        guard.chmod(0o644)
        return guard

    def test_child_env_sets_canonical_values_and_strips_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            guard = self.install_guard(home)
            capture = home / "capture.json"
            inherited = {
                "HOME": "/hostile/home",
                "GIG_DISK_HEADROOM_KIB": "0",
                "GIG_HOST_STATE_DIR": "/hostile/host-state",
                "GIG_STATE_DIR": "/hostile/lane-state",
                "GIG_IGNORE_DISK_PRESSURE_BLOCK": "1",
                "GIG_IGNORE_DISK_WRITERS_STOP": "true",
                "DISK_CONTROL_STATE_DIR": "/hostile/control",
                "OPENCLAW_STATE_DIR": "/hostile/openclaw",
                "LIFE_MANAGER_HOST_STATE_DIR": "/hostile/life-manager",
                "STUB_CAPTURE": str(capture),
                "STUB_RESULT": "0",
            }
            with patch.dict(os.environ, inherited, clear=False):
                self.assertTrue(MODULE._disk_preflight(home, guard))

            record = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(record["argv"], [str(guard), "/usr/bin/true"])
            self.assertEqual(record["isolated"], 1)
            child_env = record["env"]
            self.assertEqual(child_env["HOME"], str(home))
            self.assertEqual(child_env["LIFE_MANAGER_DISK_HEADROOM_KIB"], "524288")
            self.assertEqual(child_env["LIFE_MANAGER_HOST_STATE_DIR"],
                             str(home / ".local/state/life-manager/state"))
            self.assertEqual(child_env["LIFE_MANAGER_PRODUCER_STATE_DIR"],
                             str(home / ".local/state/life-manager/affiliate"))
            for key in (
                "GIG_IGNORE_DISK_PRESSURE_BLOCK", "GIG_IGNORE_DISK_WRITERS_STOP",
                "DISK_CONTROL_STATE_DIR", "OPENCLAW_STATE_DIR",
                "GIG_DISK_HEADROOM_KIB", "GIG_HOST_STATE_DIR", "GIG_STATE_DIR",
            ):
                self.assertNotIn(key, child_env)

    def test_default_home_comes_from_passwd_not_inherited_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            self.install_guard(home)
            capture = home / "capture.json"
            with (
                patch.dict(os.environ, {"HOME": "/hostile/home",
                                        "STUB_CAPTURE": str(capture)}, clear=False),
                patch.object(MODULE.pwd, "getpwuid",
                             return_value=SimpleNamespace(pw_dir=str(home))),
                patch.object(MODULE.os, "getuid", return_value=9876),
            ):
                self.assertTrue(MODULE._disk_preflight(guard=home / GUARD_RELATIVE))
            record = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(record["env"]["LIFE_MANAGER_HOST_STATE_DIR"],
                             str(home / ".local/state/life-manager/state"))

    def test_consumer_passes_each_flag_path_to_guard_boundary(self) -> None:
        # Policy semantics belong to Life Manager's guard suite; this stub only
        # verifies the Affiliate consumer's canonical path/env composition.
        for flag, reason in (("disk-writers.stop", "disk_writers_stop"),
                              ("disk-pressure.block", "disk_pressure_block")):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                host_state = home / ".local/state/life-manager/state"
                lane_state = home / ".local/state/life-manager/affiliate"
                host_state.mkdir(parents=True)
                lane_state.mkdir(parents=True)
                (host_state / flag).write_text("blocked\n", encoding="utf-8")
                guard = self.install_guard(home)
                capture = home / "capture.json"
                with patch.dict(os.environ, {
                    "HOME": "/hostile/home", "GIG_DISK_HEADROOM_KIB": "0",
                    "GIG_IGNORE_DISK_PRESSURE_BLOCK": "1",
                    "GIG_IGNORE_DISK_WRITERS_STOP": "1",
                    "STUB_CAPTURE": str(capture),
                }, clear=False):
                    self.assertFalse(MODULE._disk_preflight(home, guard))
                receipt = json.loads(
                    (lane_state / "state/disk-headroom.json").read_text(encoding="utf-8")
                )
                self.assertEqual(receipt["reason"], reason)
                self.assertEqual(receipt["required_bytes"], 524288 * 1024)

    def test_missing_or_unreadable_guard_has_no_browser_or_profile_effect(self) -> None:
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary) / "home"
                profile = Path(temporary) / "profile"
                home.mkdir()
                if unreadable:
                    guard = self.install_guard(home)
                    guard.chmod(stat.S_IRUSR ^ stat.S_IRUSR)
                real_import = builtins.__import__

                def reject_browser_import(name, *args, **kwargs):
                    if name == "cloakbrowser":
                        raise AssertionError("cloakbrowser imported before disk preflight")
                    return real_import(name, *args, **kwargs)

                with (
                    patch.object(MODULE, "_canonical_home", return_value=home),
                    patch.object(MODULE, "_GUARD", home / GUARD_RELATIVE),
                    patch.dict(os.environ, {"AFFILIATE_BROWSER_PROFILE": str(profile),
                                            "HOME": "/hostile/home"}, clear=False),
                    patch("builtins.__import__", side_effect=reject_browser_import),
                ):
                    self.assertEqual(MODULE.main(), 1)
                self.assertFalse(profile.exists())

    def test_invalid_port_fails_before_profile_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profile"
            real_import = builtins.__import__

            def reject_browser_import(name, *args, **kwargs):
                if name == "cloakbrowser":
                    raise AssertionError("cloakbrowser imported before port validation")
                return real_import(name, *args, **kwargs)

            with (
                patch.object(MODULE, "_disk_preflight", return_value=True),
                patch.dict(os.environ, {"AFFILIATE_CDP_PORT": "0",
                                        "AFFILIATE_BROWSER_PROFILE": str(profile)},
                           clear=False),
                patch("builtins.__import__", side_effect=reject_browser_import),
            ):
                self.assertEqual(MODULE.main(), 1)
            self.assertFalse(profile.exists())


if __name__ == "__main__":
    unittest.main()
