import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / "runtime/run-with-timeout.py"


class RunWithTimeoutTest(unittest.TestCase):
    def test_returns_child_status(self):
        result = subprocess.run(
            [sys.executable, str(RUNNER), "5", sys.executable, "-c", "raise SystemExit(7)"],
            check=False,
        )
        self.assertEqual(result.returncode, 7)

    def test_timeout_returns_124(self):
        result = subprocess.run(
            [sys.executable, str(RUNNER), "0.01", sys.executable, "-c", "import time; time.sleep(5)"],
            check=False,
            timeout=2,
        )
        self.assertEqual(result.returncode, 124)

    def test_external_signal_reaches_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            stopped = Path(directory) / "stopped"
            grandchild = (
                "import pathlib,signal,time,sys; "
                "ready=pathlib.Path(sys.argv[1]); stopped=pathlib.Path(sys.argv[2]); "
                "signal.signal(signal.SIGTERM, lambda *_: (stopped.write_text('yes'), sys.exit(0))); "
                "ready.write_text('yes'); time.sleep(30)"
            )
            child = (
                "import subprocess,sys; "
                "raise SystemExit(subprocess.call([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]]))"
            )
            process = subprocess.Popen([
                sys.executable, str(RUNNER), "30", sys.executable, "-c", child,
                grandchild, str(ready), str(stopped),
            ])
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=3), 143)
            self.assertTrue(stopped.exists())


if __name__ == "__main__":
    unittest.main()
