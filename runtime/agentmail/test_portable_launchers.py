import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PortableAgentmailLauncherTest(unittest.TestCase):
    def test_agentmail_launchers_discover_node(self):
        for relative in (
            "runtime/agentmail/nudge-tick.sh",
            "runtime/agentmail/replier-tick.sh",
            "runtime/agentmail/launch.sh",
        ):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("command -v node", source, relative)
            self.assertIn('"status":"setup_required"', source, relative)
            self.assertNotIn("/opt/homebrew/bin/node", source, relative)


if __name__ == "__main__":
    unittest.main()
