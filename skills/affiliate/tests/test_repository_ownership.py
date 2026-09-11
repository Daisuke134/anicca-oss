from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_ROOT = REPO_ROOT / "skills" / "affiliate"
LEGACY_ROOT = SKILL_ROOT / "legacy"


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
                self.assertNotIn("." + "openclaw/.env", body, path.as_posix())
                self.assertNotIn("." + "hermes/.env", body, path.as_posix())

    def test_retired_legacy_tree_is_absent(self) -> None:
        self.assertFalse(LEGACY_ROOT.exists())


if __name__ == "__main__":
    unittest.main()
