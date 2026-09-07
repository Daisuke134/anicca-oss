from __future__ import annotations

import hashlib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = REPO_ROOT / "skills" / "affiliate"
LEGACY_ROOT = SKILL_ROOT / "legacy"
MANIFEST = LEGACY_ROOT / "SHA256SUMS"
DEPENDENCY_MANIFEST = LEGACY_ROOT / "DEPENDENCIES.sha256"

PRESERVED_FILES = {
    "affiliate-cli.sh",
    "affiliate-healthcheck.sh",
    "affiliate_verify.py",
    "launch_affiliate_browser.py",
    "launchd/ai.anicca.affiliate-core-healthcheck.plist",
    "measure_commission.py",
    "producer.sh",
    "run.sh",
    "tests/test_affiliate_verify.py",
    "tests/test_measure_commission.py",
}
DEPENDENCY_FILES = {"vendor/ytdlp-parse-shared-lib/ytdlp_parse.py"}


def manifest_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            digest, relative = line.split(maxsplit=1)
            entries[relative] = digest
    return entries


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RepositoryOwnershipTests(unittest.TestCase):
    def test_affiliate_has_no_competing_launchd_installer(self) -> None:
        self.assertFalse((SKILL_ROOT / "scripts/install-release.sh").exists())

    def test_canonical_skill_is_migration_only_and_active_files_are_portable(self) -> None:
        text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("name: affiliate\n", text)
        self.assertIn("MIGRATION_ONLY", text)
        self.assertIn("MACOS_LOCAL_ONLY", text)
        self.assertIn("LIFE_MANAGER_STATE_HOME", text)
        self.assertIn("LIFE_MANAGER_DATA_HOME", text)
        self.assertIn("lm-loop reconcile", text)

        for path in SKILL_ROOT.rglob("*"):
            if (path.is_file()
                    and "legacy" not in path.relative_to(SKILL_ROOT).parts
                    and "tests" not in path.relative_to(SKILL_ROOT).parts
                    and "state" not in path.relative_to(SKILL_ROOT).parts
                    and "__pycache__" not in path.relative_to(SKILL_ROOT).parts
                    and path.suffix != ".pyc"):
                body = path.read_text(encoding="utf-8")
                self.assertNotIn("/" + "Users/anicca", body, path.as_posix())
                self.assertNotIn("profitable-claude", body, path.as_posix())

    def test_legacy_manifest_covers_exact_preserved_files(self) -> None:
        entries = manifest_entries(MANIFEST)
        dependencies = manifest_entries(DEPENDENCY_MANIFEST)
        self.assertEqual(set(entries), PRESERVED_FILES)
        self.assertEqual(set(dependencies), DEPENDENCY_FILES)
        for relative, expected in {**entries, **dependencies}.items():
            preserved = LEGACY_ROOT / relative
            self.assertTrue(preserved.is_file(), relative)
            self.assertEqual(sha256(preserved), expected, relative)

        payload = {
            path.relative_to(LEGACY_ROOT).as_posix()
            for path in LEGACY_ROOT.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.relative_to(LEGACY_ROOT).parts
            and path.suffix != ".pyc"
        }
        self.assertEqual(
            payload,
            PRESERVED_FILES | DEPENDENCY_FILES | {"SHA256SUMS", "DEPENDENCIES.sha256"},
        )


if __name__ == "__main__":
    unittest.main()
