import os
import subprocess
import tempfile
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "skills/writer-agent/scripts"


class WriterContentRuntimeContractTest(unittest.TestCase):
    def test_registry_managed_writer_has_no_static_launchd_owner(self):
        registry = json.loads((ROOT / "config/loop-registry.json").read_text())["loops"]
        labels = {
            row["label"] for loop_id, row in registry.items()
            if loop_id.startswith(("article-", "writer-"))
        }
        static_labels = {
            path.stem for path in SCRIPTS.rglob("*.plist")
        }
        legacy_jobs = json.loads(
            (ROOT / "skills/earn/gig/config/launchd-jobs.json").read_text()
        )["jobs"]
        legacy_labels = {row["label"] for row in legacy_jobs}
        self.assertEqual(static_labels, set())
        self.assertEqual(labels & static_labels, set())
        self.assertEqual(labels & legacy_labels, set())
        self.assertIn("ai.anicca.article-repair-candidate", labels)
        self.assertEqual(list((ROOT / "skills/writer-agent").rglob("*.plist.example")), [])
        self.assertEqual(list(SCRIPTS.glob("install-writer-*.sh")), [])
        self.assertFalse((SCRIPTS / "install-zenn-deferred-worker.sh").exists())

    def test_active_content_paths_are_repository_or_writer_state_owned(self):
        paths = (
            SCRIPTS / "propose.sh",
            SCRIPTS / "run.sh",
            SCRIPTS / "seo-gate.sh",
            SCRIPTS / "publish-note.sh",
            SCRIPTS / "freshness-gate.sh",
            SCRIPTS / "extract-daily-lesson.sh",
            SCRIPTS / "identity-gate.sh",
            SCRIPTS / "deslop-gate.sh",
            SCRIPTS / "eval-gate.sh",
            SCRIPTS / "conscience-gate.sh",
            SCRIPTS / "render-verify-draft.sh",
            SCRIPTS / "article_weekly_audit.py",
            SCRIPTS / "note-publish/set-eyecatch-draft.py",
            ROOT / "skills/_shared/propose-and-rewrite.sh",
            ROOT / "skills/_shared/lib/account-history.sh",
            ROOT / "skills/_shared/lib/experience-log.sh",
            ROOT / "skills/_shared/lib/verbatim-guard.sh",
        )
        for path in paths:
            with self.subTest(path=path):
                body = path.read_text(encoding="utf-8")
                self.assertNotIn("$HOME/.openclaw", body)
                self.assertNotIn("${ANICCA_HOME", body)

    def test_shared_gate_refuses_legacy_runtime_root(self):
        with tempfile.TemporaryDirectory() as temp:
            result = subprocess.run(
                ["bash", str(SCRIPTS / "freshness-gate.sh"), "portable title"],
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "HOME": temp,
                    "LIFE_MANAGER_REPO": str(ROOT),
                    "LIFE_MANAGER_ENV_FILE": str(Path(temp) / "missing.env"),
                    "ARTICLE_STATE_DIR": str(Path(temp) / ".openclaw/state"),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("OK no history", result.stdout)
            self.assertIn("refuses legacy", result.stderr)

    def test_entrypoints_have_no_operator_identity_defaults(self):
        body = "\n".join(
            (SCRIPTS / name).read_text(encoding="utf-8")
            for name in ("propose.sh", "run.sh", "seo-gate.sh")
        )
        for identity in (
            "anicca-daisuke",
            "anicca_301094325e",
            "anicca_ai",
            "aniccabuddha.substack.com",
            "aniccaai.substack.com",
            "note.com/anicca123",
        ):
            with self.subTest(identity=identity):
                self.assertNotIn(identity, body)

    def test_propose_runs_on_clean_home_with_repo_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "writer"
            library = state / "content-library"
            experience = state / "experience-log"
            library.mkdir(parents=True)
            experience.mkdir(parents=True)
            (library / "pattern-article.jsonl").write_text(
                '{"source_id":"own-1","language":"ja","structural_principle":"fact then lesson"}\n'
            )
            today = subprocess.check_output(["date", "-u", "+%Y-%m-%d"], text=True).strip()
            (experience / f"{today}.jsonl").write_text(
                '{"useful_for_content":"y","summary":"shipped a migration"}\n'
            )
            result = subprocess.run(
                ["bash", str(SCRIPTS / "propose.sh"), "--channel", "zenn"],
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "HOME": temp,
                    "LIFE_MANAGER_REPO": str(ROOT),
                    "ARTICLE_STATE_DIR": str(state),
                    "ZENN_ACCOUNT": "example-writer",
                    "LIFE_MANAGER_ENV_FILE": str(Path(temp) / "missing.env"),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"account":"example-writer"', result.stdout)
            self.assertIn(str(ROOT / "skills/writer-agent/reference/default-persona.md"), result.stdout)
            self.assertNotIn("Anicca", result.stdout)
            self.assertNotIn("Daisuke", result.stdout)
            self.assertNotIn("persona-anicca.md", result.stdout)

    def test_seo_gate_uses_installation_owned_link_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            article = Path(temp) / "article.md"
            article.write_text(
                "## one\n## two\n## three\n" + "本文" * 800
                + "\nhttps://writer.example/next\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "bash", str(SCRIPTS / "seo-gate.sh"),
                    "--title", "題" * 32,
                    "--meta", "説明" * 60,
                    "--markdown-file", str(article),
                    "--lang", "ja",
                ],
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "HOME": temp,
                    "LIFE_MANAGER_REPO": str(ROOT),
                    "ARTICLE_STATE_DIR": str(Path(temp) / "writer"),
                    "LIFE_MANAGER_ENV_FILE": str(Path(temp) / "missing.env"),
                    "ARTICLE_INTERNAL_LINK_URLS": "https://writer.example/",
                    "ARTICLE_CTA_URLS": "https://writer.example/next",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("int_links=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
