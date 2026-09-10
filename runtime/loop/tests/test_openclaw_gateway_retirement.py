import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class OpenClawGatewayRetirementTests(unittest.TestCase):
    def test_broken_openclaw_scheduler_is_not_reimplemented_as_a_second_fundraiser(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        fundraiser = registry["loops"]["fundraiser"]
        self.assertEqual(fundraiser["label"], "ai.anicca.fundraiser")
        self.assertEqual(fundraiser["cadence"], {"start_interval_seconds": 1800})
        self.assertEqual(fundraiser["provider_route"], "shared-agent-runner")

    def test_no_managed_loop_uses_the_openclaw_gateway(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        self.assertIn("ai.anicca.tier2-agent-diagnose", registry["retired_labels"])
        self.assertNotIn("tier2-agent-diagnose", registry["loops"])
        for loop_id, row in registry["loops"].items():
            rendered = json.dumps(row).lower()
            with self.subTest(loop_id=loop_id):
                self.assertNotIn("18789", rendered)
                self.assertNotEqual(row.get("provider_route"), "openclaw")

    def test_direct_managed_entrypoints_do_not_execute_openclaw(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        for loop_id, row in registry["loops"].items():
            entrypoint = ROOT / row["entrypoint"]
            if not entrypoint.is_file():
                continue
            source = entrypoint.read_text(errors="replace").lower()
            with self.subTest(loop_id=loop_id):
                self.assertNotIn("openclaw message", source)
                self.assertNotIn("openclaw_json", source)
                self.assertNotIn("openclaw status", source)

    def test_gateway_cannot_retire_while_protected_gig_dependencies_remain(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        storefront = (ROOT / "skills/earn/gig/scripts/storefront_direct.py").read_text()
        brake = (ROOT / "skills/earn/gig/scripts/gig_brake.sh").read_text()
        protected_dependency_exists = (
            "args.openclaw" in storefront or "GIG_BRAKE_OPENCLAW" in brake
        )
        if protected_dependency_exists:
            self.assertNotIn("ai.openclaw.gateway", registry["retired_labels"])

    def test_gig_outcome_watch_uses_canonical_state_and_shared_telegram(self):
        source = (ROOT / "tools/gig-outcome-watch/notify.sh").read_text()
        legacy_home = "$HOME/." + "open" + "claw/.env"
        legacy_binary = "/opt/homebrew/bin/" + "open" + "claw"
        self.assertIn("LIFE_MANAGER_STATE_ROOT", source)
        self.assertIn("skills/_shared/send-telegram.sh", source)
        self.assertIn("$HOME/.local/state/life-manager/.env", source)
        self.assertNotIn(legacy_home, source)
        self.assertNotIn(legacy_binary, source)


if __name__ == "__main__":
    unittest.main()
