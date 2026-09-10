import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_runner import command_for  # noqa: E402


class ClaudeEffectPermissionsTest(unittest.TestCase):
    def _command(self, *, read_only: bool) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                task_class="escalation-agent",
                workdir=root,
                read_only=read_only,
            )
            return command_for(
                "claude-direct",
                "claude",
                {},
                {"model": "claude-sonnet-5"},
                args,
                "fixture prompt",
                {},
                root / "result.json",
            )

    def test_effect_owner_bypasses_interactive_permission_prompts(self):
        self.assertIn("--dangerously-skip-permissions", self._command(read_only=False))

    def test_read_only_decision_does_not_bypass_permissions(self):
        self.assertNotIn("--dangerously-skip-permissions", self._command(read_only=True))


if __name__ == "__main__":
    unittest.main()
