#!/usr/bin/env python3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProducerPortabilityTest(unittest.TestCase):
    def test_producer_uses_repository_pipeline_and_declared_dependencies(self):
        source = (ROOT / "producer.sh").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertNotIn(".cache/anicca-clones", source)
        self.assertNotIn("git clone", source)
        self.assertNotIn("/opt/homebrew/bin/python3", source)
        self.assertIn("LIFE_MANAGER_PYTHON", source)
        self.assertIn('"status":"setup_required"', source)
        self.assertIn("faster-whisper", requirements)
        self.assertIn("yt-dlp", requirements)


if __name__ == "__main__":
    unittest.main()
