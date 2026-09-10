import argparse
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_runner import command_for  # noqa: E402


class WriterRepairRouteTest(unittest.TestCase):
    def test_repair_route_keeps_workspace_cage_and_explicit_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); schema = root / "schema.json"; schema.write_text("{}")
            args = argparse.Namespace(
                task_class="writer-repair-agent", schema=schema, workdir=root,
                image=[], read_only=False, codex_resume_session_id="thread-123",
            )
            command = command_for(
                "codex", "codex", {},
                {"provider": "codex", "model": "gpt-5.6-terra", "effort": "medium"},
                args, "repair", {}, root / "result.json",
                prompt_via_stdin=True,
            )
            self.assertNotIn("--ephemeral", command)
            sandbox = command.index("--sandbox")
            self.assertEqual(command[sandbox:sandbox + 2], ["--sandbox", "workspace-write"])
            self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", command)
            self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", command)
            self.assertIn("sandbox_workspace_write.network_access=false", command)
            self.assertEqual(command[-3:], ["resume", "thread-123", "-"])


if __name__ == "__main__":
    unittest.main()
