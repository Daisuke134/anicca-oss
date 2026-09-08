import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/writer-agent/scripts/writer-runtime-env.sh"


class WriterRuntimeEnvTest(unittest.TestCase):
    def run_source(self, env):
        return subprocess.run(
            ["bash", "-c", f'source "{SCRIPT}" && printf "%s\\n" "$ARTICLE_ROOT|$ARTICLE_STATE_DIR|$WRITER_LOG_DIR|$LIFE_MANAGER_ENV_FILE"'],
            text=True,
            capture_output=True,
            env={**os.environ, **env},
        )

    def test_one_contract_resolves_repo_state_log_and_env(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            result = self.run_source({"HOME": str(home), "LIFE_MANAGER_REPO": str(ROOT)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "|".join([
                str(ROOT / "skills/writer-agent"),
                str(home / ".local/state/life-manager/writer"),
                str(home / ".local/state/life-manager/writer/logs"),
                str(home / ".local/state/life-manager/.env"),
            ]))

    def test_dotenv_cannot_redirect_runtime_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_file = root / "life-manager.env"
            env_file.write_text(
                "ARTICLE_ROOT=/tmp/evil-code\nARTICLE_STATE_DIR=/tmp/evil-state\n"
                "WRITER_LOG_DIR=/tmp/evil-log\nLIFE_MANAGER_REPO=/tmp/evil-repo\n"
            )
            expected_state = root / "writer"
            result = self.run_source({
                "HOME": str(root),
                "LIFE_MANAGER_REPO": str(ROOT),
                "ARTICLE_ROOT": str(ROOT / "skills/writer-agent"),
                "ARTICLE_STATE_DIR": str(expected_state),
                "WRITER_LOG_DIR": str(expected_state / "logs"),
                "LIFE_MANAGER_ENV_FILE": str(env_file),
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "|".join([
                str(ROOT / "skills/writer-agent"), str(expected_state),
                str(expected_state / "logs"), str(env_file),
            ]))

    def test_legacy_state_or_log_override_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            result = self.run_source({
                "HOME": temp,
                "LIFE_MANAGER_REPO": str(ROOT),
                "ARTICLE_STATE_DIR": str(Path(temp) / ".openclaw/state"),
            })
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refuses legacy", result.stderr)

    def test_all_fourteen_registry_entrypoints_use_the_shared_contract(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())
        rows = {
            loop_id: row for loop_id, row in registry["loops"].items()
            if loop_id.startswith(("article-", "writer-"))
        }
        self.assertEqual(len(rows), 14)
        for loop_id, row in rows.items():
            with self.subTest(loop_id=loop_id):
                source = (ROOT / row["entrypoint"]).read_text(errors="replace")
                self.assertIn("writer-runtime-env.sh", source)
                self.assertNotIn("/.openclaw", source)
                self.assertNotIn("/.hermes", source)
                self.assertEqual(row["state_root"], "~/.local/state/life-manager/writer")
                self.assertEqual(row["log_root"], "~/.local/state/life-manager/writer/logs")


if __name__ == "__main__":
    unittest.main()
