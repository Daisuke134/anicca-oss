import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("ebook_runner.py")
SPEC = importlib.util.spec_from_file_location("ebook_runner", MODULE_PATH)
ebook_runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(ebook_runner)


class EbookPortabilityTest(unittest.TestCase):
    def test_default_asset_root_is_life_manager_owned(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.dict(os.environ, {"HOME": temp}, clear=False):
                os.environ.pop("LM_EBOOK_ASSET_ROOT", None)
                os.environ.pop("XDG_DATA_HOME", None)
                root = ebook_runner.default_asset_root()
        self.assertEqual(root, Path(temp) / ".local/share/life-manager/ebook-assets")
        self.assertNotIn("openclaw", str(root).lower())
        self.assertNotIn("hermes", str(root).lower())
        self.assertNotIn("monk-factory", str(root).lower())

    def test_explicit_asset_root_wins(self):
        with mock.patch.dict(os.environ, {"LM_EBOOK_ASSET_ROOT": "/tmp/ebook-assets"}):
            self.assertEqual(ebook_runner.default_asset_root(), Path("/tmp/ebook-assets"))

    def test_watercolor_clip_paths_share_one_asset_contract(self):
        root = Path("/portable/assets")
        paths = ebook_runner.watercolor_clip_paths(root)
        self.assertEqual(len(paths), 6)
        self.assertTrue(all(path.parent == root / "watercolor-monk/clips" for path in paths))

    def test_english_render_routes_to_repo_owned_heygen_adapter(self):
        script = {
            "product_id": "ebook-en", "account_id": "product:ebook-en",
            "cta": "Read The Anicca Reset", "campaign_id": "campaign-1",
            "creative_id": "creative-1", "source_mechanism_ids": ["source-1"],
            "declared_mutation": "action", "body": "Breathe slowly.",
            "renderer_id": "heygen-avatar-iv", "hook": "Breathe",
        }
        pack = {
            "product_id": "ebook-en", "renderer_id": "heygen-avatar-iv",
            "slots_jst": ["08:00"], "accounts": [],
        }
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(ebook_runner, "load_ebook_packs", return_value={"en": pack}), \
                mock.patch.object(ebook_runner, "ScriptLedger") as ledger, \
                mock.patch.object(ebook_runner, "render_heygen", return_value={
                    "renderer_id": "heygen-avatar-iv", "state": "setup_required",
                    "missing": ["LM_EBOOK_EN_HEYGEN_AVATAR_ID"], "external_effects": [],
                }) as renderer:
            ledger.return_value.get.return_value = script
            receipt = ebook_runner.run(
                engine=Path(temp), product="ebook-en", slot_at="2026-09-12T08:00:00+09:00",
                script_id="script-1", ledger_path=Path(temp) / "scripts.db",
                state_root=Path(temp) / "runs", render_output=Path(temp) / "out.mp4",
            )
        renderer.assert_called_once_with(script="Breathe slowly.", output=Path(temp) / "out.mp4")
        self.assertEqual(receipt["state"], "setup_required")
        self.assertEqual(receipt["external_effects"], [])


if __name__ == "__main__":
    unittest.main()
