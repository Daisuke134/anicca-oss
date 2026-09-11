import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


class LatenessEntrypointContractTest(unittest.TestCase):
    def test_state_and_logs_live_outside_immutable_release(self):
        source = (ROOT / "skills/anicca-life-manager/scripts/run.sh").read_text()
        self.assertIn('LOG="$STATE_ROOT/logs/run.log"', source)
        self.assertIn("unset ANICCA_HOME OPENCLAW_ENV_FILE", source)
        self.assertEqual(source.count("unset ANICCA_HOME OPENCLAW_ENV_FILE"), 2)
        self.assertNotIn("migrate-legacy-lateness-state.py", source)
        self.assertIn('PYTHON_BIN="${LIFE_MANAGER_PYTHON:-python3}"', source)
        self.assertIn('"$LIFE_MANAGER_REPO/runtime/run-with-timeout.py"', source)
        self.assertNotIn("/opt/homebrew", source)
        self.assertNotIn("TIMEOUT_BIN", source)
        self.assertIn("LATENESS_STATUS=$?", source)
        self.assertIn('exit "$LATENESS_STATUS"', source)
        self.assertNotIn('LOG="$SKILL/state/run.log"', source)
        self.assertNotIn("openclaw cron", source.lower())

        checker = (ROOT / "skills/anicca-life-manager/scripts/lateness_check.py").read_text()
        self.assertIn('LOOP_STATE_DIR = LIFE_MANAGER_STATE_ROOT / "state"', checker)
        for name in (
            "heartbeat_log.jsonl",
            "nudge_sent.json",
            "active_call_loop.json",
            "renraku_sent.json",
        ):
            self.assertIn(f'LOOP_STATE_DIR / "{name}"', checker)
        self.assertNotIn('parent.parent / "state"', checker)


if __name__ == "__main__":
    unittest.main()
