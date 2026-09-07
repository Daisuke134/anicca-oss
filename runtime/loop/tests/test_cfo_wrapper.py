import unittest
from pathlib import Path
import json
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[3]


class CfoWrapperContractTest(unittest.TestCase):
    def test_registry_gate_uses_release_registry_and_canonical_loop_id(self):
        wrapper = (ROOT / "skills/cfo/run.sh").read_text()
        self.assertIn("registry_enforce_or_exit life-manager-cfo-hourly", wrapper)
        self.assertIn("unset CEO_STATE_DIR", wrapper)

    def test_registry_reader_uses_the_canonical_interval_cadence(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Path(directory) / "registry.json"
            registry.write_text(json.dumps({"loops": {"life-manager-cfo-hourly": {
                "cadence": {"start_interval_seconds": 3600},
            }}}))
            result = subprocess.run([
                sys.executable, str(ROOT / "lib/registry_enforce_read.py"),
                str(registry), "life-manager-cfo-hourly",
            ], check=True, capture_output=True, text=True)
        self.assertIn("RESULT=ok", result.stdout)
        self.assertIn("BASE_INTERVAL=3600", result.stdout)


if __name__ == "__main__":
    unittest.main()
