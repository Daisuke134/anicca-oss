import json
import os
import subprocess
import tempfile
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

    def test_affiliate_transitive_entrypoint_uses_shared_telegram(self):
        source = (ROOT / "skills/affiliate/scripts/local_loop.py").read_text().lower()
        self.assertNotIn("openclaw message send", source)
        self.assertNotIn("shutil.which(\"openclaw\")", source)
        self.assertNotIn("8547730585", source)
        self.assertIn("send_via_shared_client", source)

    def test_clone_has_no_executable_openclaw_transport_or_provider(self):
        shared_transports = {
            "skills/earn/gig/scripts/freelancer_bid_watch.py": "send_via_shared_client",
            "apps/life-manager/scripts/personalized-action-e2e.js": "sendMessage",
            "apps/life-manager/lib/outbound-guardian.js": "./telegram.js",
            "apps/life-manager/lib/connector-ticket-telegram.js": "./telegram.js",
            "apps/life-manager/lib/connector-coverage-telegram.js": "notifyTelegramReport",
        }
        forbidden = (
            "openclaw message send", 'spawn("openclaw"', 'command("openclaw"',
            '["openclaw", "message"', 'provider == "openclaw"',
        )
        for relative, expected in shared_transports.items():
            source = (ROOT / relative).read_text()
            with self.subTest(path=relative):
                self.assertIn(expected, source)
                for needle in forbidden:
                    self.assertNotIn(needle, source.lower())

        runner = (ROOT / "runtime/agent-runner/agent_runner.py").read_text().lower()
        config = json.loads((ROOT / "runtime/agent-runner/config.json").read_text())
        self.assertNotIn("openclaw", runner)
        self.assertNotIn("openclaw", config["providers"])
        self.assertFalse((ROOT / "apps/life-manager/skill-life-manager/openclaw").exists())
        self.assertFalse((ROOT / "uninstall.sh").exists())

    def test_gateway_retires_after_protected_gig_dependencies_are_removed(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        storefront = (ROOT / "skills/earn/gig/scripts/storefront_direct.py").read_text()
        brake = (ROOT / "skills/earn/gig/scripts/gig_brake.sh").read_text()
        self.assertNotIn("args.openclaw", storefront)
        self.assertNotIn("GIG_BRAKE_OPENCLAW", brake)
        self.assertNotIn("openclaw message send", brake)
        self.assertIn("send_via_shared_client", storefront)
        self.assertIn("_shared/send-telegram.sh", brake)
        self.assertIn("ai.openclaw.gateway", registry["retired_labels"])

    def test_gig_brake_sends_through_configured_shared_sender(self):
        brake = ROOT / "skills/earn/gig/scripts/gig_brake.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = root / "telegram-argv.txt"
            sender = root / "send-telegram.sh"
            sender.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$GIG_BRAKE_TEST_CAPTURE\"\n"
                "printf 'TELEGRAM_SENT=true MSGID=123\\n'\n"
            )
            sender.chmod(0o700)
            env = {
                **os.environ,
                "GIG_OPERATOR_BRAKE_FILE": str(root / "operator.brake"),
                "GIG_BRAKE_LOG": str(root / "brake.log"),
                "GIG_BRAKE_TELEGRAM_SENDER": str(sender),
                "GIG_BRAKE_TELEGRAM": "42",
                "GIG_BRAKE_TEST_CAPTURE": str(capture),
            }
            completed = subprocess.run(
                [str(brake), "raise", "--owner", "test", "--reason", "transport", "--ttl-minutes", "1"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            sent = capture.read_text().splitlines()
            self.assertIn("GIG BRAKE RAISED", sent[0])
            self.assertEqual(sent[-1], "42")

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
