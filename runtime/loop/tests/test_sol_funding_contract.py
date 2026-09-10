import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class SolFundingContractTests(unittest.TestCase):
    def test_registry_owns_one_finite_portable_sol_funding_wake(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        row = registry["loops"]["sol-funding"]
        self.assertEqual(row["label"], "ai.anicca.sol-funding")
        self.assertEqual(row["entrypoint"], "skills/earn/sol-funding-owner")
        self.assertEqual(row["cadence"], {"start_interval_seconds": 60})
        self.assertEqual(row["effect_class"], "money")
        self.assertEqual(row["state_root"], "~/.local/state/life-manager/sol-funding")
        self.assertIn("com.anicca.sol-funding", registry["retired_labels"])
        self.assertFalse((ROOT / "skills/earn/com.anicca.sol-funding.plist").exists())
        self.assertFalse((ROOT / "skills/earn/sol-funding-daemon.sh").exists())

    def test_owner_loads_private_env_and_uses_managed_python(self):
        owner = ROOT / "skills/earn/sol-funding-owner"
        self.assertTrue(os.access(owner, os.X_OK))
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            fake_python = Path(temporary) / "python"
            env_file.write_text(
                "ANICCA_SOLANA_KEY=sentinel-key\n"
                "SWAP_RECIPIENT=0x1111111111111111111111111111111111111111\n"
            )
            fake_python.write_text("#!/bin/sh\nenv\n")
            fake_python.chmod(0o700)
            result = subprocess.run(
                [str(owner)],
                env={
                    **os.environ,
                    "LIFE_MANAGER_REPO": str(ROOT),
                    "LIFE_MANAGER_ENV_FILE": str(env_file),
                    "LIFE_MANAGER_PYTHON": str(fake_python),
                },
                check=True,
                capture_output=True,
                text=True,
            )
        self.assertIn("ANICCA_SOLANA_KEY=sentinel-key", result.stdout)
        self.assertIn(
            "SWAP_RECIPIENT=0x1111111111111111111111111111111111111111",
            result.stdout,
        )

    def test_runtime_locks_solders_and_source_has_no_user_recipient_default(self):
        requirements = (ROOT / "requirements-runtime.txt").read_text().splitlines()
        self.assertIn("solders==0.27.1", requirements)
        source = (ROOT / "skills/earn/sol-to-usdc.py").read_text()
        self.assertIn('os.environ.get("SWAP_RECIPIENT")', source)
        self.assertNotIn('os.environ.get("SWAP_RECIPIENT",', source)


if __name__ == "__main__":
    unittest.main()
