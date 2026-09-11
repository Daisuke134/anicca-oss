#!/usr/bin/env python3
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "capafy-ig-marketing-daily.sh"


class ApprovedHyperFramesWiringTest(unittest.TestCase):
    def test_step3_uses_approved_hyperframes_and_andrew_voice(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--task-class marketing-agent", source)
        self.assertIn("$LIFE_MANAGER_REPO/skills/video/hyperframes/capafy-o13-review/", source)
        self.assertIn("hyperframes@0.8.8 render", source)
        self.assertIn("one foreground exec session", source)
        self.assertIn("identical across two probes at least 2 seconds apart", source)
        self.assertIn("edge-tts --voice en-US-AndrewNeural", source)
        self.assertIn("four listing-specific 1080x1920 scenes", source)
        self.assertIn("zero scene-boundary crossings", source)
        self.assertIn("repo-owned test fixture or immutable live output receipt", source)
        self.assertIn("inspect full-resolution frames from all four scenes", source)
        self.assertNotIn("say -v Samantha", source)
        self.assertNotIn("skills/video/canonical-renderer/render.py", source)

    def test_runtime_has_no_repo_external_faceless_renderer(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("~/.claude/skills/faceless-money-factory", source)
        self.assertNotIn("~/.agents/skills/faceless-money-factory", source)

    def test_profile_writer_is_repo_owned(self):
        source = SCRIPT.read_text(encoding="utf-8")
        profile_writer = SCRIPT.parent / "scripts/setup_profile.py"

        self.assertIn("$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/setup_profile.py", source)
        self.assertNotIn("~/.agents/skills/ig-account-create", source)
        self.assertTrue(profile_writer.is_file())

    def test_live_pass_selects_once_and_requires_new_native_reel_before_success(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("STEP1 SELECTED (deterministic caller; do not call selector again)", source)
        self.assertIn("evidence-ready listing selection failed", source)
        self.assertIn("live pass produced no verified native Reel", source)
        self.assertIn("Do not reject a valid tier1 session merely because", source)
        self.assertIn('"$PYTHON_BIN"', source)
        self.assertIn("$LIFE_MANAGER_REPO/skills/earn/marketing-engine/poster.py", source)
        self.assertNotIn("/opt/homebrew/bin/python3", source)
        self.assertNotIn("~/.cache/instagrapi-venv/bin/python", source)
        self.assertNotIn("use session_owner=instagrapi from the supplied Capafy state", source)
        self.assertIn("--commit-agent-id", source)
        self.assertLess(source.index("VERIFIED_POST="), source.index("--commit-agent-id"))
        self.assertIn('(\"listing_name\", \"caption\", \"hook\")', source)
        self.assertIn("mandatory experiment evidence", source)


if __name__ == "__main__":
    unittest.main()
