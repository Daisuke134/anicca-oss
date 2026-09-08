import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/writer-agent/scripts/writer-runtime-env.sh"
SCRIPTS = SCRIPT.parent
sys.path.insert(0, str(SCRIPTS))
from writer_runtime_paths import life_manager_env_file  # noqa: E402
from writer_report_worker import telegram_api_transport  # noqa: E402


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
                "LIFE_MANAGER_PYTHON=/tmp/evil-python\nWRITER_BROWSER_PYTHON=/tmp/evil-browser\n"
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
            python_result = subprocess.run(
                ["bash", "-c", f'source "{SCRIPT}" && printf "%s|%s" "$LIFE_MANAGER_PYTHON" "$WRITER_BROWSER_PYTHON"'],
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "LIFE_MANAGER_REPO": str(ROOT),
                    "LIFE_MANAGER_ENV_FILE": str(env_file),
                    "LIFE_MANAGER_PYTHON": "/managed/python",
                },
            )
            self.assertEqual(python_result.stdout, "/managed/python|/managed/python")

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

    def test_python_contract_resolves_default_and_override_env(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.assertEqual(
                life_manager_env_file({}, home=home),
                home / ".local/state/life-manager/.env",
            )
            self.assertEqual(
                life_manager_env_file(
                    {"LIFE_MANAGER_ENV_FILE": "~/private/life-manager.env"}, home=home
                ),
                Path.home() / "private/life-manager.env",
            )

    def test_browser_python_comes_from_life_manager_managed_runtime(self):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{SCRIPT}" && printf "%s|%s" "$WRITER_BROWSER_PYTHON" "$WRITER_CLOAK_PYTHON"',
            ],
            text=True,
            capture_output=True,
            env={**os.environ, "LIFE_MANAGER_REPO": str(ROOT), "LIFE_MANAGER_PYTHON": "/managed/python"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "/managed/python|/managed/python")

    def test_writer_scripts_have_no_legacy_or_host_specific_python(self):
        legacy = (
            ".openclaw/skills/_shared/venv-cloak/bin/python3",
            "/opt/homebrew/bin/python3",
        )
        scripts = ROOT / "skills/writer-agent/scripts"
        offenders = []
        for path in scripts.rglob("*"):
            if not path.is_file() or path.suffix == ".md" or "__pycache__" in path.parts:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            if any(value in body for value in legacy):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_writer_credential_consumers_have_no_openclaw_env_dependency(self):
        consumers = (
            "opportunity_response.py",
            "writer_report_worker.py",
            "article-completion-notify.py",
            "self-improve-notify.py",
            "_shared/pii_scan.py",
            "publish-devto.sh",
            "publish-substack.sh",
            "publish-zenn.sh",
            "publish-note.sh",
            "rotation-effect-audit.sh",
            "_shared/publish-substack-mermaid.sh",
        )
        for relative in consumers:
            with self.subTest(relative=relative):
                body = (SCRIPTS / relative).read_text(encoding="utf-8")
                self.assertNotIn(".openclaw/.env", body)
        for relative in (
            "publish-devto.sh",
            "publish-substack.sh",
            "publish-zenn.sh",
            "publish-note.sh",
            "rotation-effect-audit.sh",
            "_shared/publish-substack-mermaid.sh",
        ):
            self.assertIn("writer-runtime-env.sh", (SCRIPTS / relative).read_text())
        self.assertNotIn(
            '"openclaw",\n                "message"',
            (SCRIPTS / "self-improve-notify.py").read_text(),
        )

    def test_telegram_transport_accepts_canonical_life_manager_token_name(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / "life-manager.env"
            env_file.write_text("LM_TELEGRAM_BOT_TOKEN=fixture-token\n")
            transport = telegram_api_transport("12345", env_file=env_file)
            self.assertTrue(callable(transport))


if __name__ == "__main__":
    unittest.main()
