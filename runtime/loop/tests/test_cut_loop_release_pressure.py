import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class CutLoopReleasePressureTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
