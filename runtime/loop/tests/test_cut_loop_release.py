import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEPENDENCY_ROOTS = (
    Path("."),
    Path("runtime/compute-proxy"),
    Path("runtime/agentmail"),
    Path("apps/life-manager"),
    Path("skills/earn/x402-sell"),
    Path("services/x402-endpoint"),
)


class CutLoopReleaseTest(unittest.TestCase):
    def test_connector_sparse_release_includes_shared_browser_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loops = root / "loops"
            agents = root / "agents"
            agents.mkdir()
            npm = root / "npm"
            npm.write_text("#!/bin/sh\nmkdir -p node_modules\n")
            npm.chmod(0o755)

            result = subprocess.run(
                ["/bin/bash", str(ROOT / "bin/cut-loop-release.sh"), "origin/main"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "LOOPS_ROOT": str(loops),
                    "LOOPS_RELEASE_PATHS": "bin config runtime/loop runtime/agent-runner skills/_shared skills/connector apps/life-manager",
                    "LOOPS_ACTIVATE_CURRENT": "0",
                    "LIFE_MANAGER_LAUNCH_AGENTS_DIR": str(agents),
                    "LIFE_MANAGER_DISK_PRESSURE_FILE": str(root / "no-pressure"),
                    "NPM_BIN": str(npm),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            release = next((loops / "releases").iterdir())
            self.assertTrue((release / "runtime/browser/target-lease.cjs").is_file())

    def test_release_can_be_built_without_changing_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loops = root / "loops"
            previous = loops / "releases" / "previous"
            previous.mkdir(parents=True)
            (previous / "RELEASE.json").write_text('{"sha":"old"}\n')
            current = loops / "current"
            current.symlink_to(previous)
            npm = root / "npm"
            npm.write_text("#!/bin/sh\nmkdir -p node_modules\n")
            npm.chmod(0o755)

            result = subprocess.run(
                ["/bin/bash", str(ROOT / "bin/cut-loop-release.sh"), "origin/main"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "LOOPS_ROOT": str(loops),
                    "LOOPS_RELEASE_PATHS": "runtime/loop",
                    "LOOPS_ACTIVATE_CURRENT": "0",
                    "LIFE_MANAGER_DISK_PRESSURE_FILE": str(root / "no-pressure"),
                    "NPM_BIN": str(npm),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(current.resolve(), previous.resolve())
            releases = [p for p in (loops / "releases").iterdir() if p != previous]
            self.assertEqual(len(releases), 1)
            self.assertTrue((releases[0] / "RELEASE.json").is_file())
            self.assertIn("current unchanged", result.stdout)

    def test_release_reuses_matching_sealed_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loops = root / "loops"
            donor = loops / "releases" / "donor"
            for relative in DEPENDENCY_ROOTS:
                package = donor / relative
                package.mkdir(parents=True, exist_ok=True)
                (package / "package-lock.json").write_bytes(
                    (ROOT / relative / "package-lock.json").read_bytes()
                )
                modules = package / "node_modules"
                modules.mkdir()
                (modules / ".package-lock.json").write_text("{}\n")
                (modules / "donor-marker").write_text("sealed")
            (donor / "RELEASE.json").write_text(
                '{"sha":"%s","release_paths":"ALL"}\n' % ("a" * 40)
            )
            (loops / "current").symlink_to(donor)
            npm = root / "npm"
            npm.write_text("#!/bin/sh\nexit 99\n")
            npm.chmod(0o755)
            agents = root / "agents"
            agents.mkdir()

            result = subprocess.run(
                ["/bin/bash", str(ROOT / "bin/cut-loop-release.sh"), "origin/main"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "LOOPS_ROOT": str(loops),
                    "LOOPS_KEEP_RELEASES": "2",
                    "LIFE_MANAGER_LAUNCH_AGENTS_DIR": str(agents),
                    "LIFE_MANAGER_DISK_PRESSURE_FILE": str(root / "no-pressure"),
                    "NPM_BIN": str(npm),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            release = (loops / "current").resolve()
            self.assertTrue(all(
                path.is_symlink() or path.stat().st_mode & 0o222 == 0
                for path in [release, *release.rglob("*")]
            ))
            for relative in DEPENDENCY_ROOTS:
                self.assertEqual(
                    (release / relative / "node_modules/donor-marker").read_text(), "sealed"
                )

    def test_release_ignores_an_empty_matching_dependency_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loops = root / "loops"
            donor = loops / "releases" / "donor"
            for relative in DEPENDENCY_ROOTS:
                package = donor / relative
                package.mkdir(parents=True, exist_ok=True)
                (package / "package-lock.json").write_bytes(
                    (ROOT / relative / "package-lock.json").read_bytes()
                )
                (package / "node_modules").mkdir()
            (donor / "RELEASE.json").write_text('{"sha":"%s"}\n' % ("a" * 40))
            (loops / "current").symlink_to(donor)
            calls = root / "npm.calls"
            npm = root / "npm"
            npm.write_text(f'#!/bin/sh\nprintf "%s\n" "$PWD" >> "{calls}"\nmkdir -p node_modules\nprintf "{{}}\\n" > node_modules/.package-lock.json\n')
            npm.chmod(0o755)

            result = subprocess.run(
                ["/bin/bash", str(ROOT / "bin/cut-loop-release.sh"), "origin/main"],
                cwd=ROOT,
                env={**os.environ, "LOOPS_ROOT": str(loops), "LOOPS_KEEP_RELEASES": "2", "LIFE_MANAGER_DISK_PRESSURE_FILE": str(root / "no-pressure"), "NPM_BIN": str(npm)},
                capture_output=True, text=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(calls.read_text().splitlines()), len(DEPENDENCY_ROOTS))

    def test_release_builds_locked_root_and_agentmail_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "npm.calls"
            npm = root / "npm"
            npm.write_text(
                f'#!/bin/sh\nprintf "%s|%s\\n" "$PWD" "$*" >> "{calls}"\n'
                'mkdir -p node_modules\n')
            npm.chmod(0o755)
            agents = root / "agents"
            agents.mkdir()
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "bin/cut-loop-release.sh"), "origin/main"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "LOOPS_ROOT": str(root / "loops"),
                    "LOOPS_KEEP_RELEASES": "1",
                    "LIFE_MANAGER_LAUNCH_AGENTS_DIR": str(agents),
                    "LIFE_MANAGER_DISK_PRESSURE_FILE": str(root / "no-pressure"),
                    "NPM_BIN": str(npm),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = calls.read_text().splitlines()
            self.assertEqual(len(recorded), len(DEPENDENCY_ROOTS))
            self.assertTrue(recorded[0].endswith("|ci --omit=dev --ignore-scripts"))
            self.assertIn("/runtime/compute-proxy|ci --omit=dev --ignore-scripts", recorded[1])
            self.assertIn("/runtime/agentmail|ci --omit=dev --ignore-scripts", recorded[2])
            self.assertIn("/apps/life-manager|ci --omit=dev --ignore-scripts", recorded[3])
            self.assertIn("/skills/earn/x402-sell|ci --omit=dev --ignore-scripts", recorded[4])
            self.assertIn("/services/x402-endpoint|ci --omit=dev --ignore-scripts", recorded[5])

    def test_reconciler_pins_captured_main_sha_when_origin_moves_during_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            loops = root / "loops"
            old_release = loops / "releases" / "old"
            new_release = loops / "releases" / "new"
            old_release.mkdir(parents=True)
            new_release.mkdir(parents=True)
            current = loops / "current"
            current.symlink_to(old_release)
            captured_sha = "a" * 40
            (old_release / "RELEASE.json").write_text(
                '{"sha":"%s","release_paths":"ALL"}\n' % ("b" * 40)
            )
            (root / "origin.sha").write_text(captured_sha)
            cutter_arg = root / "cutter.arg"
            calls = root / "lm-loop.calls"
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -C ] && [ \"$3\" = fetch ]; then exit 0; fi\n"
                "if [ \"$1\" = -C ] && [ \"$3\" = rev-parse ]; then\n"
                f"  cat {cutter_arg.parent / 'origin.sha'}\n"
                "  exit 0\n"
                "fi\n"
                "if [ \"$1\" = -C ] && [ \"$3\" = merge-base ]; then exit 0; fi\n"
                "if [ \"$1\" = -C ] && [ \"$3\" = diff ]; then exit 1; fi\n"
                "exit 1\n"
            )
            fake_git.chmod(0o755)
            cutter = old_release / "bin" / "cut-loop-release.sh"
            cutter.parent.mkdir()
            cutter.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$1\" > {cutter_arg}\n"
                f"printf '%s\\n' \"{'c' * 40}\" > {root / 'origin.sha'}\n"
                f"mkdir -p {new_release / 'bin'}\n"
                f"printf '%s\\n' '{{\"sha\":\"{captured_sha}\",\"release_paths\":\"ALL\"}}' > {new_release / 'RELEASE.json'}\n"
                f"printf '%s\\n' '#!/bin/sh' 'printf \"%s|%s\\\\n\" \"$LIFE_MANAGER_RELEASE_ROOT\" \"$*\" >> {calls}' 'exit 0' > {new_release / 'bin' / 'lm-loop'}\n"
                f"chmod +x {new_release / 'bin' / 'lm-loop'}\n"
                f"ln -sfn {new_release} {current}\n"
            )
            cutter.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "bin/reconcile-agent-runner-release.sh")],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "LIFE_MANAGER_SOURCE_REPO": str(root),
                    "LOOPS_ROOT": str(loops),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(cutter_arg.read_text().strip(), captured_sha)
            self.assertEqual((root / "origin.sha").read_text().strip(), "c" * 40)
            reconciles = calls.read_text().splitlines()
            self.assertEqual(len(reconciles), 3)
            self.assertTrue(
                all(line.startswith(f"{new_release.resolve()}|") for line in reconciles),
                reconciles,
            )
            self.assertEqual(
                [line.split("|", 1)[1] for line in reconciles],
                [
                    "reconcile shared-agent-runner --loaded-idle-only --loop-id hf-gig-apply-direct",
                    "reconcile shared-agent-runner --include-running --loop-id hf-gig-reply-detector",
                    "reconcile deterministic --loaded-idle-only --loop-id hf-gig-storefront-direct --loop-id hf-gig-paid-direct --loop-id life-manager-disk-cleanup",
                ],
            )

    def test_reconciler_reuses_complete_release_for_docs_only_main(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            loops = root / "loops"
            release = loops / "releases" / "current-release"
            release.mkdir(parents=True)
            current = loops / "current"
            current.symlink_to(release)
            current_sha = "b" * 40
            main_sha = "a" * 40
            (release / "RELEASE.json").write_text(
                '{"sha":"%s","release_paths":"ALL"}\n' % current_sha
            )
            cutter_called = root / "cutter.called"
            calls = root / "lm-loop.calls"
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -C ] && [ \"$3\" = fetch ]; then exit 0; fi\n"
                f"if [ \"$1\" = -C ] && [ \"$3\" = rev-parse ]; then printf '%s\\n' {main_sha}; exit 0; fi\n"
                "if [ \"$1\" = -C ] && [ \"$3\" = merge-base ]; then exit 0; fi\n"
                "if [ \"$1\" = -C ] && [ \"$3\" = diff ]; then exit 0; fi\n"
                "exit 1\n"
            )
            fake_git.chmod(0o755)
            cutter = release / "bin" / "cut-loop-release.sh"
            cutter.parent.mkdir()
            cutter.write_text(f"#!/bin/sh\ntouch {cutter_called}\nexit 99\n")
            cutter.chmod(0o755)
            lm_loop = release / "bin" / "lm-loop"
            lm_loop.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> {calls}\n"
            )
            lm_loop.chmod(0o755)

            result = subprocess.run(
                ["/bin/bash", str(ROOT / "bin/reconcile-agent-runner-release.sh")],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "LIFE_MANAGER_SOURCE_REPO": str(root),
                    "LOOPS_ROOT": str(loops),
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(cutter_called.exists())
            self.assertEqual(len(calls.read_text().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
