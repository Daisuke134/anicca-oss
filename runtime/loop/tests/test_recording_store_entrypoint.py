import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "skills/life-manager-video/run-store-recordings.sh"


class RecordingStoreEntrypointTest(unittest.TestCase):
    def test_missing_optional_telnyx_configuration_is_a_clean_no_effect_run(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.pop("TELNYX_API_KEY", None)
            env["HOME"] = directory
            env["LIFE_MANAGER_ENV_FILE"] = str(Path(directory) / "missing.env")
            result = subprocess.run(
                [str(ENTRYPOINT)],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            '{"status":"skipped","reason":"telnyx_not_configured"}\n',
        )
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
