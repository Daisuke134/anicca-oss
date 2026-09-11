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


if __name__ == "__main__":
    unittest.main()
