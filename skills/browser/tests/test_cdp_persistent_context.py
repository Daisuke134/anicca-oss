import builtins
import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "cdp_persistent_context.py"
ENSURE = Path(__file__).parents[1] / "ensure_provision_browser.sh"
SPEC = importlib.util.spec_from_file_location("cdp_persistent_context", SCRIPT)
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
    ignored = filename == "disk-pressure.block" and os.environ.get("LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK") == "1"
    if (host_state / filename).is_file() and not ignored:
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
raise SystemExit(1 if reason else 0)
"""


class CdpPersistentContextPreflightTests(unittest.TestCase):
    def test_browser_has_a_finite_renderer_process_limit(self) -> None:
        self.assertIn(
            'f"--renderer-process-limit={renderer_limit}"',
            SCRIPT.read_text(encoding="utf-8"),
        )

    def test_persistent_context_disables_code_sign_clone(self) -> None:
        self.assertIn(
            '"--disable-features=MacAppCodeSignClone"',
            SCRIPT.read_text(encoding="utf-8"),
        )

    def install_guard(self, home: Path) -> Path:
        guard = home / GUARD_RELATIVE
        guard.parent.mkdir(parents=True)
        guard.write_text(STUB, encoding="utf-8")
        guard.chmod(0o644)
        return guard

    def reject_browser_import(self):
        real_import = builtins.__import__

        def reject(name, *args, **kwargs):
            if name == "cloakbrowser":
                raise AssertionError("cloakbrowser imported before disk preflight")
            return real_import(name, *args, **kwargs)

        return reject

    def test_child_env_is_canonical_and_isolated_from_hostile_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            guard = self.install_guard(home)
            capture = root / "capture.json"
            hostile = root / "hostile"
            hostile.mkdir()
            marker = hostile / "sitecustomize-ran"
            (hostile / "sitecustomize.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('hostile')\n",
                encoding="utf-8",
            )
            environment = {
                "HOME": "/hostile/home",
                "PYTHONPATH": str(hostile),
                "GIG_DISK_HEADROOM_KIB": "0",
                "GIG_HOST_STATE_DIR": "/hostile/host-state",
                "GIG_STATE_DIR": "/hostile/lane-state",
                "GIG_IGNORE_DISK_PRESSURE_BLOCK": "0",
                "GIG_IGNORE_DISK_WRITERS_STOP": "true",
                "DISK_CONTROL_STATE_DIR": "/hostile/control",
                "OPENCLAW_STATE_DIR": "/hostile/openclaw",
                "LIFE_MANAGER_HOST_STATE_DIR": "/hostile/life-manager",
                "STUB_CAPTURE": str(capture),
            }
            with patch.dict(os.environ, environment, clear=False):
                self.assertTrue(MODULE._disk_preflight(home, guard))
            record = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(record["argv"], [str(guard), "/usr/bin/true"])
            self.assertEqual(record["isolated"], 1)
            self.assertFalse(marker.exists())
            child_env = record["env"]
            self.assertEqual(child_env["HOME"], str(home))
            self.assertEqual(child_env["LIFE_MANAGER_DISK_HEADROOM_KIB"], "524288")
            self.assertEqual(child_env["LIFE_MANAGER_HOST_STATE_DIR"],
                             str(home / ".local/state/life-manager/state"))
            self.assertEqual(child_env["LIFE_MANAGER_PRODUCER_STATE_DIR"],
                             str(home / ".local/state/life-manager/browser-provision"))
            self.assertEqual(child_env["LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK"], "1")
            for key in (
                "GIG_IGNORE_DISK_WRITERS_STOP",
                "DISK_CONTROL_STATE_DIR", "OPENCLAW_STATE_DIR",
                "GIG_DISK_HEADROOM_KIB", "GIG_HOST_STATE_DIR", "GIG_STATE_DIR",
            ):
                self.assertNotIn(key, child_env)

    def test_preventive_pressure_is_ignored_but_hard_stop_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            host_state = home / ".local/state/life-manager/state"
            host_state.mkdir(parents=True)
            guard = self.install_guard(home)
            capture = root / "capture.json"
            (host_state / "disk-pressure.block").write_text("blocked\n", encoding="utf-8")
            with patch.dict(os.environ, {"STUB_CAPTURE": str(capture)}, clear=False):
                self.assertTrue(MODULE._disk_preflight(home, guard))
            (host_state / "disk-writers.stop").write_text("blocked\n", encoding="utf-8")
            with patch.dict(os.environ, {"STUB_CAPTURE": str(capture)}, clear=False):
                self.assertFalse(MODULE._disk_preflight(home, guard))
            receipt = json.loads(
                (home / ".local/state/life-manager/browser-provision/state/disk-headroom.json").read_text()
            )
            self.assertEqual(receipt["reason"], "disk_writers_stop")
            self.assertEqual(receipt["effect"], 0)
            self.assertEqual(receipt["required_bytes"], 524288 * 1024)

    def test_daily_driver_may_use_the_bounded_256_mib_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            guard = self.install_guard(home)
            capture = root / "capture.json"
            with patch.dict(os.environ, {
                "BROWSER_DISK_HEADROOM_KIB": "262144", "STUB_CAPTURE": str(capture)
            }, clear=False):
                self.assertTrue(MODULE._disk_preflight(home, guard))
            record = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(record["env"]["LIFE_MANAGER_DISK_HEADROOM_KIB"], "262144")

    def test_with_browser_starts_unreachable_identity_and_owns_one_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            guard = root / "guard.sh"
            ensure = root / "ensure.sh"
            calls = root / "calls"
            guard.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"guard:$1:$2:${{AI_BROWSER_HOLDER_PID:-}}\" >> {calls!s}\n"
                "case \"$1\" in\n"
                "  acquire) exit 10 ;;\n"
                "  release) exit 0 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            ensure.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"ensure:$1:${{AI_BROWSER_HOLDER_PID:-}}\" >> {calls!s}\n"
                "printf '%s\\n' http://127.0.0.1:54321\n",
                encoding="utf-8",
            )
            guard.chmod(0o755)
            ensure.chmod(0o755)
            completed = subprocess.run(
                ["bash", str(ENSURE.with_name("with-browser.sh")), "buyma:test", "--",
                 "sh", "-c", 'test "$CDP" = http://127.0.0.1:54321'],
                env={**os.environ, "AI_BROWSER_GUARD": str(guard),
                     "AI_ENSURE_PROVISION_BROWSER": str(ensure),
                     "BROWSER_WAIT_SECONDS": "1"},
                capture_output=True, text=True, check=False, timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            records = calls.read_text(encoding="utf-8").splitlines()
            self.assertRegex(records[0], r"^guard:acquire:buyma:test:\d+$")
            holder = records[0].rsplit(":", 1)[1]
            self.assertEqual(records[1], f"ensure:buyma:test:{holder}")
            self.assertEqual(records[2], "guard:release:buyma:test:")

    def test_port_zero_preflight_only_reaches_guard_without_cloak_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            guard = self.install_guard(home)
            capture = root / "capture.json"
            with (
                patch.dict(os.environ, {"HOME": "/hostile/home", "STUB_CAPTURE": str(capture)}, clear=False),
                patch.object(MODULE, "_canonical_home", return_value=home),
                patch.object(MODULE, "_GUARD", guard),
                patch("builtins.__import__", side_effect=self.reject_browser_import()),
            ):
                self.assertEqual(
                    MODULE.main(["--profile", str(root / "profile"), "--port", "0", "--preflight-only"]),
                    0,
                )
            self.assertTrue(capture.exists())

    def test_shell_preflight_precedes_profile_and_launch_side_effects(self) -> None:
        text = ENSURE.read_text(encoding="utf-8")
        launch = text.split("launch() {", 1)[1].split("\n}", 1)[0]
        preflight = launch.index('--port 0 --preflight-only')
        for effect in (
            'mkdir -p "$profile"',
            "clear_stale_singletons",
            'launchctl remove "$LABEL"',
            "sleep 1",
            "launchctl submit",
        ):
            self.assertLess(preflight, launch.index(effect))
        self.assertIn('"$CLOAK_PY" "$KEEPALIVE" --profile "$profile" --port 0 --preflight-only', launch)

    def test_missing_symlink_or_unreadable_guard_has_no_cloak_import(self) -> None:
        for kind in ("missing", "symlink", "unreadable"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                home = root / "home"
                home.mkdir()
                guard = home / GUARD_RELATIVE
                if kind == "symlink":
                    target = root / "guard-target.py"
                    target.write_text(STUB, encoding="utf-8")
                    guard.parent.mkdir(parents=True)
                    guard.symlink_to(target)
                elif kind == "unreadable":
                    self.install_guard(home).chmod(0)
                with (patch.dict(os.environ, {"HOME": "/hostile/home"}, clear=False),
                      patch.object(MODULE, "_canonical_home", return_value=home),
                      patch.object(MODULE, "_GUARD", guard),
                      patch("builtins.__import__", side_effect=self.reject_browser_import())):
                    self.assertEqual(MODULE.main(["--profile", str(root / "profile"), "--port", "9333"]), 1)

    def test_invalid_port_has_no_cloak_import_or_effect(self) -> None:
        for port in ("-1", "65536", "not-an-int"):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with (patch.object(MODULE, "_disk_preflight", return_value=True),
                      patch("builtins.__import__", side_effect=self.reject_browser_import())):
                    self.assertEqual(MODULE.main(["--profile", str(root / "profile"), "--port", port]), 1)
                self.assertFalse((root / "profile").exists())

if __name__ == "__main__":
    unittest.main()
