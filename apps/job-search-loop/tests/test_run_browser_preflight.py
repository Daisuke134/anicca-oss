import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run-browser.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")


class RunBrowserPreflightTests(unittest.TestCase):
    def _assert_mercor_rejects_before_profile_mutation(
        self, profile_kind: str, port: str | None = None
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            profile_root = home / ".cloak" / "profiles"
            daily_profile = profile_root / "job-search-daily"
            profile_root.mkdir(parents=True)
            daily_profile.mkdir()
            sentinel = daily_profile / "SingletonLock"
            sentinel.write_text("keep", encoding="utf-8")
            if profile_kind == "symlink":
                selected_profile = profile_root / "daily-alias"
                selected_profile.symlink_to(daily_profile, target_is_directory=True)
            elif profile_kind == "dotdot":
                (profile_root / "nested").mkdir()
                selected_profile = profile_root / "nested" / ".." / "job-search-daily"
            elif profile_kind == "port":
                selected_profile = profile_root / "mercor-profile"
            else:
                selected_profile = daily_profile

            wrapper = root / "apps" / "job-search-loop" / "scripts" / "run-browser.sh"
            wrapper.parent.mkdir(parents=True)
            script = TEXT
            start = script.index("if ! CANONICAL_HOME=")
            end = script.index('export HOME="$CANONICAL_HOME"', start) + len(
                'export HOME="$CANONICAL_HOME"'
            )
            wrapper.write_text(
                script[:start]
                + 'CANONICAL_HOME="$TEST_CANONICAL_HOME"\nexport HOME="$CANONICAL_HOME"'
                + script[end:],
                encoding="utf-8",
            )
            guard = root / "runtime" / "host" / "disk_admission.py"
            guard.parent.mkdir(parents=True)
            guard.write_text("raise SystemExit(0)\n", encoding="utf-8")
            chromium = (
                home
                / ".cloakbrowser"
                / "chromium-1"
                / "Chromium.app"
                / "Contents"
                / "MacOS"
                / "Chromium"
            )
            chromium.parent.mkdir(parents=True)
            chromium.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
            chromium.chmod(0o700)

            environment = os.environ.copy()
            for name in (
                "JOB_SEARCH_BROWSER_STATE_NAME",
                "JOB_SEARCH_BROWSER_PORT",
                "JOB_SEARCH_BROWSER_FINGERPRINT",
                "JOB_SEARCH_BROWSER_PROFILE",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "TEST_CANONICAL_HOME": str(home),
                    "LIFE_MANAGER_LOOP_ID": "job-search-mercor-browser",
                    "JOB_SEARCH_BROWSER_PROFILE": str(selected_profile),
                }
            )
            if port is not None:
                environment["JOB_SEARCH_BROWSER_PORT"] = port
            completed = subprocess.run(
                ["/bin/zsh", str(wrapper)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertTrue(daily_profile.is_dir())
            if profile_kind == "symlink":
                self.assertTrue(selected_profile.is_symlink())
            elif profile_kind == "port":
                self.assertFalse(selected_profile.exists())

    def test_mercor_rejects_direct_daily_profile_before_profile_mutation(self) -> None:
        self._assert_mercor_rejects_before_profile_mutation("direct")

    def test_mercor_rejects_daily_profile_symlink_alias_before_profile_mutation(self) -> None:
        self._assert_mercor_rejects_before_profile_mutation("symlink")

    def test_mercor_rejects_daily_profile_dotdot_alias_before_profile_mutation(self) -> None:
        self._assert_mercor_rejects_before_profile_mutation("dotdot")

    def test_mercor_rejects_port_9222_before_profile_mutation(self) -> None:
        self._assert_mercor_rejects_before_profile_mutation("port", port="9222")

    def test_mercor_rejects_conflicting_state_before_guard_or_profile_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            profile = home / ".cloak" / "profiles" / "conflict-profile"
            profile.mkdir(parents=True)
            sentinel = profile / "SingletonLock"
            sentinel.write_text("keep", encoding="utf-8")

            wrapper = root / "apps" / "job-search-loop" / "scripts" / "run-browser.sh"
            wrapper.parent.mkdir(parents=True)
            script = TEXT
            start = script.index("if ! CANONICAL_HOME=")
            end = script.index('export HOME="$CANONICAL_HOME"', start) + len(
                'export HOME="$CANONICAL_HOME"'
            )
            wrapper.write_text(
                script[:start]
                + 'CANONICAL_HOME="$TEST_CANONICAL_HOME"\nexport HOME="$CANONICAL_HOME"'
                + script[end:],
                encoding="utf-8",
            )
            guard_marker = root / "guard-ran"
            guard = root / "runtime" / "host" / "disk_admission.py"
            guard.parent.mkdir(parents=True)
            guard.write_text(
                "from pathlib import Path\n"
                "import os\n"
                "Path(os.environ[\"GUARD_MARKER\"]).write_text(\"ran\")\n",
                encoding="utf-8",
            )
            chromium = (
                home
                / ".cloakbrowser"
                / "chromium-1"
                / "Chromium.app"
                / "Contents"
                / "MacOS"
                / "Chromium"
            )
            chromium.parent.mkdir(parents=True)
            chromium.write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
            chromium.chmod(0o700)

            environment = os.environ.copy()
            for name in (
                "JOB_SEARCH_BROWSER_STATE_NAME",
                "JOB_SEARCH_BROWSER_PORT",
                "JOB_SEARCH_BROWSER_FINGERPRINT",
                "JOB_SEARCH_BROWSER_PROFILE",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "TEST_CANONICAL_HOME": str(home),
                    "LIFE_MANAGER_LOOP_ID": "job-search-mercor-browser",
                    "JOB_SEARCH_BROWSER_STATE_NAME": "job-search-browser",
                    "JOB_SEARCH_BROWSER_PROFILE": str(profile),
                    "GUARD_MARKER": str(guard_marker),
                }
            )
            completed = subprocess.run(
                ["/bin/zsh", str(wrapper)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertFalse(guard_marker.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_loop_id_only_reconstructs_mercor_browser_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            resume_profile = home / "mercor-resume-profile"
            resume_state = home / ".local" / "state" / "anicca" / "job-search" / "mercor" / "resume-state.json"
            resume_state.parent.mkdir(parents=True)
            resume_state.write_text(
                json.dumps({"browser": {"profile": str(resume_profile)}}),
                encoding="utf-8",
            )
            wrapper = root / "apps" / "job-search-loop" / "scripts" / "run-browser.sh"
            wrapper.parent.mkdir(parents=True)
            script = TEXT
            start = script.index("if ! CANONICAL_HOME=")
            end = script.index('export HOME="$CANONICAL_HOME"', start) + len(
                'export HOME="$CANONICAL_HOME"'
            )
            wrapper.write_text(
                script[:start]
                + 'CANONICAL_HOME="$TEST_CANONICAL_HOME"\nexport HOME="$CANONICAL_HOME"'
                + script[end:],
                encoding="utf-8",
            )
            guard = root / "runtime" / "host" / "disk_admission.py"
            guard.parent.mkdir(parents=True)
            guard_capture = root / "guard.json"
            guard.write_text(
                """
import json
import os
from pathlib import Path

Path(os.environ["GUARD_CAPTURE"]).write_text(
    json.dumps({
        "state": os.environ.get("LIFE_MANAGER_PRODUCER_STATE_DIR"),
        "pressure": os.environ.get("LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK"),
        "headroom": os.environ.get("LIFE_MANAGER_DISK_HEADROOM_KIB"),
    }),
    encoding="utf-8",
)
""",
                encoding="utf-8",
            )
            chromium = (
                home
                / ".cloakbrowser"
                / "chromium-1"
                / "Chromium.app"
                / "Contents"
                / "MacOS"
                / "Chromium"
            )
            chromium.parent.mkdir(parents=True)
            chromium_capture = root / "chromium-args.txt"
            chromium.write_text(
                '#!/bin/zsh\nprint -rl -- "$@" > "$CHROMIUM_CAPTURE"\n',
                encoding="utf-8",
            )
            chromium.chmod(0o700)

            environment = os.environ.copy()
            for name in (
                "JOB_SEARCH_BROWSER_STATE_NAME",
                "JOB_SEARCH_BROWSER_PORT",
                "JOB_SEARCH_BROWSER_FINGERPRINT",
                "JOB_SEARCH_BROWSER_PROFILE",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "TEST_CANONICAL_HOME": str(home),
                    "GUARD_CAPTURE": str(guard_capture),
                    "CHROMIUM_CAPTURE": str(chromium_capture),
                    "LIFE_MANAGER_LOOP_ID": "job-search-mercor-browser",
                    "JOB_SEARCH_BROWSER_PORT_OWNER": str(
                        Path(__file__).parents[3] / "runtime/host/browser_port_owner.py"
                    ),
                    "JOB_SEARCH_BROWSER_PORT_STATE_DIR": str(root / "port-state"),
                }
            )
            completed = subprocess.run(
                ["/bin/zsh", str(wrapper)],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(guard_capture.read_text(encoding="utf-8")),
                {
                    "state": str(home / ".local" / "state" / "life-manager" / "mercor-browser"),
                    "pressure": "1",
                    "headroom": "524288",
                },
            )
            args = chromium_capture.read_text(encoding="utf-8").splitlines()
            self.assertIn("--remote-debugging-port=9334", args)
            self.assertIn("--fingerprint=81234", args)
            self.assertIn(f"--user-data-dir={resume_profile}", args)

    def test_state_name_controls_pressure_override_at_guard_boundary(self) -> None:
        fake_guard = """
import json
import os
from pathlib import Path

Path(os.environ["GUARD_CAPTURE"]).write_text(
    json.dumps({
        "pressure": os.environ.get("LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK"),
        "writers": os.environ.get("LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP"),
        "headroom": os.environ.get("LIFE_MANAGER_DISK_HEADROOM_KIB"),
    }),
    encoding="utf-8",
)
raise SystemExit(1)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "apps" / "job-search-loop" / "scripts" / "run-browser.sh"
            wrapper.parent.mkdir(parents=True)
            shutil.copy2(SCRIPT, wrapper)
            guard = root / "runtime" / "host" / "disk_admission.py"
            guard.parent.mkdir(parents=True)
            guard.write_text(fake_guard, encoding="utf-8")

            def capture(state_name: str) -> dict[str, str | None]:
                output = root / f"{state_name}.json"
                environment = os.environ.copy()
                environment.update(
                    {
                        "GUARD_CAPTURE": str(output),
                        "GIG_IGNORE_DISK_PRESSURE_BLOCK": "inherited",
                        "GIG_IGNORE_DISK_WRITERS_STOP": "inherited",
                        "JOB_SEARCH_BROWSER_PROFILE": str(root / "profile"),
                        "JOB_SEARCH_BROWSER_STATE_NAME": state_name,
                    }
                )
                completed = subprocess.run(
                    ["/bin/zsh", str(wrapper)],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 1, completed.stderr)
                return json.loads(output.read_text(encoding="utf-8"))

            mercor = capture("mercor-browser")
            default = capture("job-search-browser")

        self.assertEqual(mercor, {"pressure": "1", "writers": None, "headroom": "524288"})
        self.assertEqual(default, {"pressure": None, "writers": None, "headroom": "524288"})

    def test_uses_canonical_guard_and_fenced_child_environment(self) -> None:
        self.assertIn(
            '${SCRIPT_DIR:h:h:h}/runtime/host/disk_admission.py',
            TEXT,
        )
        self.assertNotIn('$CANONICAL_HOME/gig/releases/', TEXT)
        self.assertIn("/usr/bin/python3 -I", TEXT)
        self.assertIn("pwd.getpwuid", TEXT)
        self.assertIn("LIFE_MANAGER_DISK_HEADROOM_KIB=524288", TEXT)
        self.assertIn('LIFE_MANAGER_HOST_STATE_DIR="$CANONICAL_HOME/.local/state/life-manager/state"', TEXT)
        self.assertIn(
            'LIFE_MANAGER_PRODUCER_STATE_DIR="$CANONICAL_HOME/.local/state/life-manager/job-search-browser"',
            TEXT,
        )
        unset_block = TEXT[TEXT.index("unset "):TEXT.index("\nif [[", TEXT.index("unset "))]
        for name in (
            "GIG_IGNORE_DISK_PRESSURE_BLOCK",
            "GIG_IGNORE_DISK_WRITERS_STOP",
            "LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK",
            "LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP",
            "DISK_CONTROL_STATE_DIR",
            "OPENCLAW_STATE_DIR",
        ):
            self.assertIn(name, TEXT)
            self.assertIn(name, unset_block)

    def test_guard_is_before_profile_chromium_and_exec_effects(self) -> None:
        guard_invocation = '/usr/bin/python3 -I "$DISK_GUARD" /usr/bin/true'
        guard = TEXT.find(guard_invocation)
        self.assertGreaterEqual(guard, 0)
        for effect in (
            'PROFILE="',
            'mkdir -p "$PROFILE"',
            'chmod 700 "$PROFILE"',
            "CHROMIUM_BIN=",
            'exec /usr/bin/python3 -I "$PORT_OWNER" run',
        ):
            self.assertLess(guard, TEXT.index(effect))
        self.assertIn(guard_invocation, TEXT)

    def test_existing_profile_and_chromium_lifecycle_stays_intact(self) -> None:
        self.assertIn(
            'PROFILE="${JOB_SEARCH_BROWSER_PROFILE-$HOME/.cloak/profiles/job-search-daily}"',
            TEXT,
        )
        self.assertIn(
            'ls -d "$HOME"/.cloakbrowser/chromium-*/Chromium.app/Contents/MacOS/Chromium(N)',
            TEXT,
        )
        for argument in (
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--use-mock-keychain",
            "--disable-sync",
            "--disable-features=MacAppCodeSignClone",
            "--no-sandbox",
            "--fingerprint=80137",
            "--fingerprint-platform=macos",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins='*'",
            "--remote-debugging-port=9222",
            '--user-data-dir="$PROFILE"',
            "about:blank",
        ):
            self.assertIn(argument, TEXT)

    def test_executes_chromium_through_shared_port_owner(self) -> None:
        self.assertIn(
            'PORT_OWNER="${JOB_SEARCH_BROWSER_PORT_OWNER-${SCRIPT_DIR:h:h:h}/runtime/host/browser_port_owner.py}"',
            TEXT,
        )
        self.assertIn(
            'exec /usr/bin/python3 -I "$PORT_OWNER" run',
            TEXT,
        )
        self.assertIn('--owner "$BROWSER_STATE_NAME"', TEXT)
        self.assertIn('-- "$CHROMIUM_BIN"', TEXT)


if __name__ == "__main__":
    unittest.main()
