#!/usr/bin/env python3
from __future__ import annotations

import pathlib
import plistlib
import tempfile
import unittest

import install_gate6_launchagents as installer


class InstallerTests(unittest.TestCase):
    def test_rewrite_preserves_schedule_label_and_logs(self):
        source = {
            "Label": "ai.anicca.marketing-score-daily",
            "ProgramArguments": ["/bin/bash", "/legacy/score.sh"],
            "StartCalendarInterval": {"Hour": 5, "Minute": 30},
            "StandardOutPath": "/tmp/out.log",
            "StandardErrorPath": "/tmp/err.log",
            "RunAtLoad": False,
        }
        rewritten = installer.rewrite_plist(source, "score")
        self.assertEqual(rewritten["Label"], source["Label"])
        self.assertEqual(rewritten["StartCalendarInterval"], source["StartCalendarInterval"])
        self.assertEqual(rewritten["StandardOutPath"], source["StandardOutPath"])
        self.assertEqual(rewritten["ProgramArguments"][-1], "score")
        self.assertIn("scheduled_runner.py", rewritten["ProgramArguments"][1])
        self.assertNotIn("legacy", " ".join(rewritten["ProgramArguments"]))

    def test_mine_label_is_owned_only_by_the_loop_registry(self):
        self.assertNotIn("ai.anicca.marketing-mine-daily", installer.MAPPINGS)

    def test_apply_is_idempotent_and_keeps_original_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "job.plist"
            backup = root / "backups"
            original = {
                "Label": "ai.anicca.marketing-score-daily",
                "ProgramArguments": ["/bin/bash", "/legacy/clip.sh"],
                "StartInterval": 86400,
            }
            path.write_bytes(plistlib.dumps(original))
            first = installer.apply_one(path, "score", backup)
            second = installer.apply_one(path, "score", backup)
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            backup_path = pathlib.Path(first["backup_path"])
            self.assertEqual(plistlib.loads(backup_path.read_bytes()), original)
            self.assertEqual(first["backup_path"], second["backup_path"])


if __name__ == "__main__":
    unittest.main()
