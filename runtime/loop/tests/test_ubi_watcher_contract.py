import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class UbiWatcherContractTests(unittest.TestCase):
    def test_registry_owns_one_finite_ubi_wake(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["ubi-watcher"]
        self.assertEqual(row["label"], "ai.anicca.ubi-watcher")
        self.assertEqual(row["entrypoint"], "skills/ubi/ubi-watcher-owner")
        self.assertEqual(row["cadence"], {"start_interval_seconds": 60})
        self.assertEqual(row["effect_class"], "money")
        self.assertEqual(row["state_root"], "~/.local/state/life-manager/ubi-watcher")
        self.assertIn("com.anicca.ubi-watcher", registry["retired_labels"])
        self.assertFalse((ROOT / "skills/ubi/com.anicca.ubi-watcher.plist").exists())
        self.assertFalse((ROOT / "skills/ubi/ubi-watcher-daemon.sh").exists())

    def test_unconfigured_owner_is_a_safe_noop(self):
        owner = ROOT / "skills/ubi/ubi-watcher-owner"
        self.assertTrue(os.access(owner, os.X_OK))
        with tempfile.TemporaryDirectory() as temporary:
            empty_env = Path(temporary) / ".env"
            empty_env.write_text("")
            result = subprocess.run(
                [str(owner)],
                env={
                    "HOME": temporary,
                    "PATH": "/usr/bin:/bin",
                    "LIFE_MANAGER_REPO": str(ROOT),
                    "LIFE_MANAGER_ENV_FILE": str(empty_env),
                    "LIFE_MANAGER_NODE": "/usr/bin/node",
                    "LIFE_MANAGER_PYTHON": "/usr/bin/python3",
                },
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ubi watcher not configured", result.stdout)

    def test_watcher_has_no_immortal_mode_or_user_wallet_default(self):
        source = (ROOT / "skills/ubi/ubi-payout-watcher.mjs").read_text()
        self.assertNotIn("--loop", source)
        self.assertNotIn("while (true)", source)
        self.assertNotIn("defer-log.jsonl", source)
        self.assertIn("process.env.UBI_TREASURY_ADDRESS", source)
        self.assertIn("process.env.LIFE_MANAGER_PYTHON", source)
        self.assertNotIn("0xb9dd3b67921b354c656523d6851537988f31dd56", source)
        self.assertNotIn("a3cdd4ec6b94f01826aaf90a6d5538a2aa8c4c21", source)


if __name__ == "__main__":
    unittest.main()
