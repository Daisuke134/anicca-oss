#!/usr/bin/env python3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PortabilityTest(unittest.TestCase):
    def test_scripts_use_repository_code_and_life_manager_state(self):
        sources = "\n".join(
            path.read_text(encoding="utf-8") for path in sorted((ROOT / "scripts").glob("*.sh"))
        )
        self.assertNotIn(".claude/skills", sources)
        self.assertNotIn(".hermes/.env", sources)
        self.assertIn("load-instance-env.sh", sources)
        self.assertIn(".local/state/life-manager/faceless-money-factory", sources)


if __name__ == "__main__":
    unittest.main()
