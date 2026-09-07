import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


AUTO = Path(__file__).resolve().parents[1]
KEY_GATE = AUTO / "scripts" / "key_health_gate.sh"
BUILD_CONFIG = AUTO / "scripts" / "build_config.py"
LINT_LISTING = AUTO / "scripts" / "lint_listing.py"
CANONICAL_PAID_ONLY_FILES = (
    AUTO / "BEST_PRACTICES.md",
    AUTO / "SKILL.md",
    AUTO / "PUBLISHING_RUNBOOK.md",
    AUTO / "references" / "pricing.md",
    AUTO.parent / "capafy" / "catalog" / "youtube-script-writer" / "LISTING.md",
)


class KeyHealthGateTest(unittest.TestCase):
    def run_gate(self, key_response, enable_alert=False, credits_remaining=9):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "curl-calls.txt"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_CURL_CALLS"
case "$*" in
  *https://openrouter.ai/api/v1/key*) printf '%s\\n' "$FAKE_KEY_RESPONSE" ;;
  *https://openrouter.ai/api/v1/credits*) printf '%s\\n' "$FAKE_CREDITS_RESPONSE" ;;
  *https://openrouter.ai/api/v1/chat/completions*) printf '%s\\n' '{"choices":[{"message":{"content":"ok"}}]}' ;;
  *) exit 1 ;;
esac
""",
                encoding="utf-8",
            )
            fake_curl.chmod(fake_curl.stat().st_mode | stat.S_IXUSR)
            alert_calls = root / "openclaw-calls.txt"
            if enable_alert:
                fake_openclaw = fake_bin / "openclaw"
                fake_openclaw.write_text(
                    """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_OPENCLAW_CALLS"
exit 0
""",
                    encoding="utf-8",
                )
                fake_openclaw.chmod(fake_openclaw.stat().st_mode | stat.S_IXUSR)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "CAPAFY_HOST_OPENROUTER_KEY": "key-must-not-print",
                    "FAKE_KEY_RESPONSE": json.dumps(key_response),
                    "FAKE_CREDITS_RESPONSE": json.dumps(
                        {"data": {"total_credits": credits_remaining, "total_usage": 0}}
                    ),
                    "FAKE_CURL_CALLS": str(calls),
                    "FAKE_OPENCLAW_CALLS": str(alert_calls),
                    "LIFE_MANAGER_STATE_HOME": str(root / "state"),
                }
            )
            if enable_alert:
                env["TELEGRAM_ALERT_CHAT_ID"] = "test-chat"
            else:
                env.pop("TELEGRAM_ALERT_CHAT_ID", None)
            result = subprocess.run(
                ["bash", str(KEY_GATE)],
                env=env,
                text=True,
                capture_output=True,
            )
            return (
                result,
                calls.read_text(encoding="utf-8") if calls.exists() else "",
                alert_calls.read_text(encoding="utf-8") if alert_calls.exists() else "",
                len(list((root / "state" / "state").glob(".capafy-funding-alert-*"))),
            )

    def test_blocks_zero_or_negative_per_key_limit_before_live_probe(self):
        for remaining in (0, -0.01):
            with self.subTest(remaining=remaining):
                result, call_text, _, _ = self.run_gate({"data": {"limit_remaining": remaining}})
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("key_limit_exhausted", output)
                self.assertNotIn("key-must-not-print", output)
                self.assertIn("https://openrouter.ai/api/v1/key", call_text)
                self.assertNotIn("https://openrouter.ai/api/v1/chat/completions", call_text)

    def test_unlimited_key_keeps_existing_balance_and_live_probe_checks(self):
        result, call_text, _, _ = self.run_gate({"data": {"limit_remaining": None}})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("live_probe=200", result.stdout)
        self.assertIn("https://openrouter.ai/api/v1/key", call_text)
        self.assertIn("https://openrouter.ai/api/v1/credits", call_text)
        self.assertIn("https://openrouter.ai/api/v1/chat/completions", call_text)

    def test_positive_key_limit_below_one_capafy_request_blocks_before_probe(self):
        result, call_text, _, _ = self.run_gate(
            {"data": {"limit_remaining": 1.50}}, credits_remaining=20
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("key_limit_below_request_headroom", output)
        self.assertNotIn("https://openrouter.ai/api/v1/chat/completions", call_text)

    def test_balance_below_default_safety_floor_blocks(self):
        result, _, _, _ = self.run_gate(
            {"data": {"limit_remaining": 10}}, credits_remaining=4.99
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("balance_too_low", result.stdout + result.stderr)

    def test_exhausted_key_calls_deduped_alert_without_printing_key(self):
        result, _, alert_text, marker_count = self.run_gate(
            {"data": {"limit_remaining": 0}}, enable_alert=True
        )
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("key_limit_exhausted", output)
        self.assertNotIn("key-must-not-print", output)
        self.assertEqual(len(alert_text.splitlines()), 1)
        self.assertIn("telegram", alert_text)
        self.assertNotIn("key-must-not-print", alert_text)
        self.assertEqual(marker_count, 1)


class PaidListingTest(unittest.TestCase):
    def listing(self, trial):
        return f"""Primary Model: Claude Sonnet 4.6 · category: ライティング · tags: writing, copy, youtube

| cycle | price | cap | trial |
| week | $9.99 | 20 | {trial} |

## Title
YouTube Script Writer
## shortDescription
Write a concise YouTube script from your topic.
## welcomeMessage
👋 I write scripts. Example: \"a launch story\"
## detailedDescription
Structured script output from your brief.
"""

    def run_tools(self, trial):
        with tempfile.TemporaryDirectory() as td:
            listing = Path(td) / "LISTING.md"
            listing.write_text(self.listing(trial), encoding="utf-8")
            lint = subprocess.run(
                [sys.executable, str(LINT_LISTING), str(listing)],
                text=True,
                capture_output=True,
            )
            build = subprocess.run(
                [
                    sys.executable,
                    str(BUILD_CONFIG),
                    str(listing),
                    "/tmp/icon.png",
                ],
                text=True,
                capture_output=True,
            )
            return lint, build

    def test_non_no_free_trial_is_rejected_by_lint_and_build(self):
        lint, build = self.run_tools("24h")
        self.assertNotEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertIn("No Free Trial", lint.stdout + lint.stderr)
        self.assertNotEqual(build.returncode, 0, build.stdout + build.stderr)
        self.assertIn("No Free Trial", build.stdout + build.stderr)

    def test_no_free_trial_builds_a_config_without_trial(self):
        lint, build = self.run_tools("No Free Trial")
        self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        config = json.loads(build.stdout)
        self.assertEqual(config["plans"], [{"cycle": "week", "price": "9.99", "cap": "20", "trial": None}])
        self.assertNotIn("edit_url", config)
        serialized = json.dumps(config, ensure_ascii=False)
        self.assertNotIn("draftKey", serialized)
        self.assertNotIn("token=", serialized)

    def test_config_file_is_private_and_contains_no_browser_url(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            listing = root / "LISTING.md"
            listing.write_text(self.listing("No Free Trial"), encoding="utf-8")
            private_dir = root / "private"
            private_dir.mkdir(mode=0o755)
            output = private_dir / "cfg_one.json"
            build = subprocess.run(
                [sys.executable, str(BUILD_CONFIG), str(listing), "/tmp/icon.png", str(output)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(private_dir.stat().st_mode), 0o700)
            config = json.loads(output.read_text(encoding="utf-8"))
            self.assertNotIn("edit_url", config)
            serialized = json.dumps(config, ensure_ascii=False)
            self.assertNotIn("draftKey", serialized)
            self.assertNotIn("token=", serialized)

    def test_config_builder_rejects_wrong_arity(self):
        build = subprocess.run(
            [sys.executable, str(BUILD_CONFIG)],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(build.returncode, 0)

    def test_canonical_paid_only_docs_have_no_enabled_free_trial_guidance(self):
        normative_patterns = (
            r"enable\s+free\s+trial",
            r"\bif\s+enable\b",
            r"free\s+trial\s*を必ず",
            r"per-plan\s+trial\s*=\s*winner",
            r"trial\s*=\s*winner\s+config",
            r"TRIAL\s+config\s*=\s*COPY\s+THE\s+WINNER",
            r"copy\s+billings?[^\n]*\btrial\b",
        )
        for path in CANONICAL_PAID_ONLY_FILES:
            text = path.read_text(encoding="utf-8")
            for pattern in normative_patterns:
                self.assertIsNone(
                    re.search(pattern, text, re.I),
                    f"normative free-trial guidance remains: {pattern} in {path}",
                )

        listing = CANONICAL_PAID_ONLY_FILES[-1].read_text(encoding="utf-8")
        rows = re.findall(
            r"\|\s*(day|week|month)\s*\|\s*\$?[0-9.]+\s*\|\s*[0-9]+\s*\|\s*([^|]+)\|",
            listing,
            re.I,
        )
        self.assertTrue(rows)
        self.assertTrue(all(re.fullmatch(r"no[\s_-]+free[\s_-]+trial", trial.strip(), re.I) for _, trial in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
