from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).parents[2]
ENTRYPOINT = ROOT / "x-tweeter" / "x-tweeter-cli.sh"
SHARED = ROOT / "x-repost" / "x-repost-cli.sh"


class XTweeterEntrypointTests(unittest.TestCase):
    def test_independent_original_role_uses_own_state_and_contract(self) -> None:
        self.assertTrue(ENTRYPOINT.is_file(), f"missing entrypoint: {ENTRYPOINT}")
        wrapper = ENTRYPOINT.read_text(encoding="utf-8")
        shared = SHARED.read_text(encoding="utf-8")

        self.assertIn("X_LOOP_ROLE=original", wrapper)
        self.assertIn(".local/state/life-manager/social-x/x-tweeter", wrapper)
        self.assertIn("X_REPOST_FORCE_KIND=original", wrapper)
        self.assertIn("X_REPOST_DISABLE_AFFILIATE=1", wrapper)
        self.assertIn("original_contract.py", shared)
        self.assertIn("original-payload.json", shared)
        self.assertIn('X_REPOST_FORCE_KIND:-', shared)
        self.assertNotIn("original_ratio", wrapper)

    def test_chinese_source_mode_is_explicit_and_does_not_invoke_mediacrawler(self) -> None:
        wrapper = ENTRYPOINT.read_text(encoding="utf-8")
        shared = SHARED.read_text(encoding="utf-8")

        self.assertIn("X_REPOST_SOURCE_MODE=chinese-public", wrapper)
        self.assertIn("X_REPOST_FORCE_LANGUAGE=en", wrapper)
        self.assertIn("X_REPOST_CANDIDATES_FILE", wrapper)
        self.assertIn("chinese_source_collect.py", wrapper)
        self.assertIn("X_TWEETER_CANDIDATE_MAX_AGE_SECONDS", wrapper)
        self.assertIn("candidate_count", wrapper)
        self.assertNotIn("mediacrawler", wrapper.lower())
        self.assertIn('X_REPOST_CANDIDATES_FILE:-', shared)


if __name__ == "__main__":
    unittest.main()
