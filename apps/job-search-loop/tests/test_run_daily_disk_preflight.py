from pathlib import Path
import unittest


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "run-daily.sh"
).read_text(encoding="utf-8")


class RunDailyDiskPreflightTests(unittest.TestCase):
    def test_disk_gate_precedes_evidence_and_model_work(self):
        guard = SCRIPT.index("disk_admission.py")
        run_id = SCRIPT.index('RUN_ID="daily-')
        reporting = SCRIPT.index("job_search_loop.application_reporting")
        orchestrator = SCRIPT.index("job_search_loop.browser_agent.orchestrator")
        self.assertLess(guard, run_id)
        self.assertLess(guard, reporting)
        self.assertLess(guard, orchestrator)

    def test_gate_honors_global_stop_flags_and_uses_512_mib_floor(self):
        self.assertIn("LIFE_MANAGER_DISK_HEADROOM_KIB=524288", SCRIPT)
        self.assertIn("LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK", SCRIPT)
        self.assertIn("LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP", SCRIPT)
        self.assertIn('exit 75', SCRIPT)


if __name__ == "__main__":
    unittest.main()
