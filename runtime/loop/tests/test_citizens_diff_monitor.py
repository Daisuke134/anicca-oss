import hashlib
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/self/spawn/scripts/citizens-diff-monitor.sh"


class CitizensDiffMonitorTest(unittest.TestCase):
    def test_explicit_registry_is_read_only_and_monitor_state_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = root / "product-state" / "citizens.json"
            state = root / "monitor-state"
            registry.parent.mkdir()
            original = b'[{"id":"citizen-1"}]\n'
            registry.write_bytes(original)
            before = hashlib.sha256(original).hexdigest()

            env = {
                **os.environ,
                "CITIZENS_REGISTRY_PATH": str(registry),
                "LIFE_MANAGER_STATE_ROOT": str(state),
                "CITIZENS_DIFF_INTERVAL": "60",
            }
            process = subprocess.Popen(
                ["/bin/bash", str(SCRIPT)], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (state / "citizens-baseline.json").exists() and (state / "logs/citizens-diff-monitor.log").exists():
                        break
                    time.sleep(0.05)
                self.assertTrue((state / "citizens-baseline.json").exists())
                self.assertTrue((state / "citizens-diff-monitor.pid").exists())
                self.assertTrue((state / "logs/citizens-diff-monitor.log").exists())
                self.assertEqual((state / "citizens-baseline.json").read_bytes(), original)
                self.assertEqual(hashlib.sha256(registry.read_bytes()).hexdigest(), before)
                self.assertFalse((registry.parent / "citizens-baseline.json").exists())
            finally:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=2)

            self.assertFalse((state / "citizens-diff-monitor.pid").exists())


if __name__ == "__main__":
    unittest.main()
