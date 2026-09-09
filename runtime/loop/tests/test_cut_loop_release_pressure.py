import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class CutLoopReleasePressureTest(unittest.TestCase):
    def run_cut(self, repo: Path, home: Path, paths: str, **extra_env: str):
        pressure = home / ".openclaw" / "state" / "disk-pressure.block"
        pressure.parent.mkdir(parents=True, exist_ok=True)
        pressure.write_text('{"tier":"PRESSURE"}\n', encoding="utf-8")
        loops = home / "loops"
        result = subprocess.run(
            ["bash", str(repo / "bin/cut-loop-release.sh"), "HEAD"],
            cwd=repo,
            env={
                **os.environ,
                "HOME": str(home),
                "LOOPS_ROOT": str(loops),
                "LIFE_MANAGER_DISK_PRESSURE_FILE": str(pressure),
                "LIFE_MANAGER_SOURCE_REPO": str(repo),
                "LOOPS_RELEASE_PATHS": paths,
                "NPM_BIN": "",
                **extra_env,
            },
            capture_output=True,
            text=True,
            check=False,
        )
        return result, loops

    def test_pressure_flag_blocks_before_git_or_release_write(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        script = repo / "bin" / "cut-loop-release.sh"

        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            pressure = home / ".openclaw" / "state" / "disk-pressure.block"
            pressure.parent.mkdir(parents=True)
            pressure.write_text('{"tier":"CRITICAL"}\n', encoding="utf-8")
            loops = home / "loops"

            result = subprocess.run(
                ["bash", str(script), "HEAD"],
                cwd=home,
                env={**os.environ, "HOME": str(home), "LOOPS_ROOT": str(loops)},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn("disk pressure", result.stderr.lower())
            self.assertFalse((loops / "releases").exists())

    def test_pressure_flag_allows_measured_bounded_sparse_release(self) -> None:
        repo = Path(__file__).resolve().parents[3]

        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            result, loops = self.run_cut(
                repo, home, "runtime/loop/runtime_event.py"
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            release = (loops / "current").resolve()
            self.assertTrue((release / "runtime/loop/runtime_event.py").is_file())
            self.assertTrue((release / "RELEASE.json").is_file())

    def test_pressure_flag_rejects_whitespace_only_paths_before_release_write(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as raw_home:
            result, loops = self.run_cut(repo, Path(raw_home), " \t ")
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertFalse((loops / "releases").exists())

    def test_pressure_flag_rejects_sparse_archive_above_ceiling(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as raw_home:
            result, loops = self.run_cut(
                repo,
                Path(raw_home),
                "runtime/loop/runtime_event.py",
                LOOPS_PRESSURE_MAX_ARCHIVE_BYTES="1",
            )
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn("exceeds bounded ceiling", result.stderr)
            self.assertFalse((loops / "releases").exists())

    def test_pressure_measurement_uses_parsed_bin_closure_with_tabs(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as raw_home:
            result, loops = self.run_cut(
                repo, Path(raw_home), "bin\truntime/loop/runtime_event.py"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            release = (loops / "current").resolve()
            self.assertTrue((release / "bin/lm-loop-run").is_file())
            self.assertTrue((release / "skills/_shared/browser-state-backup.sh").is_file())

    def test_pressure_measurement_failure_creates_no_release_root(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as raw_home:
            result, loops = self.run_cut(repo, Path(raw_home), "missing/path")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((loops / "releases").exists())


if __name__ == "__main__":
    unittest.main()
