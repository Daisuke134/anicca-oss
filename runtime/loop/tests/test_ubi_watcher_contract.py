import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.loop.lm_loop_apply import _plist


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

    def test_generated_plist_projects_managed_runtime_paths(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        with patch("runtime.loop.lm_loop_apply.shutil.which", return_value="/managed/bin/node"):
            payload = _plist("ubi-watcher", registry["loops"]["ubi-watcher"], Path("/tmp/loops/releases/20260910T000000-aaaaaaaa"), "a" * 40)
        import plistlib
        environment = plistlib.loads(payload)["EnvironmentVariables"]
        self.assertEqual(environment["LIFE_MANAGER_NODE"], "/managed/bin/node")
        self.assertTrue(Path(environment["LIFE_MANAGER_PYTHON"]).is_absolute())
        self.assertTrue(Path(environment["LIFE_MANAGER_ENV_FILE"]).is_absolute())

    def test_owner_rejects_configured_address_that_differs_from_signer(self):
        owner = ROOT / "skills/ubi/ubi-watcher-owner"
        with tempfile.TemporaryDirectory() as temporary:
            fake_node = Path(temporary) / "node"
            fake_node.write_text("#!/bin/sh\ncase \"$2\" in evm) printf '0x%s\\n' \"$(printf 1%.0s $(seq 1 64))\" ;; evm-address) printf '0x2222222222222222222222222222222222222222\\n' ;; esac\n")
            fake_node.chmod(0o755)
            empty_env = Path(temporary) / ".env"
            empty_env.write_text("")
            result = subprocess.run(
                [str(owner)],
                env={
                    "HOME": temporary,
                    "PATH": "/usr/bin:/bin",
                    "LIFE_MANAGER_REPO": str(ROOT),
                    "LIFE_MANAGER_ENV_FILE": str(empty_env),
                    "LIFE_MANAGER_NODE": str(fake_node),
                    "BLOCKRUN_WALLET_KEY": "0x" + "1" * 64,
                    "UBI_TREASURY_ADDRESS": "0x3333333333333333333333333333333333333333",
                },
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("does not match signer", result.stderr)

    def test_watcher_claims_before_send_and_never_requeues_ambiguous_failure(self):
        source = (ROOT / "skills/ubi/ubi-payout-watcher.mjs").read_text()
        self.assertIn("status: 'processing'", source)
        self.assertIn("status=eq.queued", source)
        self.assertIn("status=eq.processing", source)
        self.assertIn("'needs_review'", source)
        self.assertNotIn("status: 'queued'", source)
        self.assertLess(source.index("await claim(r)"), source.index("const tx = payWallet(to"))
        self.assertIn("retaining processing is also fail-closed", source)


if __name__ == "__main__":
    unittest.main()
