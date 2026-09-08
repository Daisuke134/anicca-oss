import importlib.util
import inspect
import sys
from pathlib import Path

from PIL import Image


MODULE = Path(__file__).parents[1] / "scripts" / "x-publish" / "x_inplace_repair.py"
sys.path.insert(0, str(MODULE.parent))
SPEC = importlib.util.spec_from_file_location("x_inplace_repair", MODULE)
assert SPEC and SPEC.loader
x_repair = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(x_repair)


def _png(path: Path, height: int, width: int = 1300) -> None:
    Image.new("RGB", (width, height), "white").save(path, format="PNG")


def test_source_media_readability_rejects_flat_and_tall_images(tmp_path):
    flat = tmp_path / "flat.png"
    tall = tmp_path / "tall.png"
    _png(flat, 110)
    _png(tall, 1441)
    receipt = x_repair._body_media_readability([flat, tall])
    assert receipt["status"] == "FAIL"
    assert any("too-flat" in item for item in receipt["violations"])
    assert any("too-tall" in item for item in receipt["violations"])


def test_source_media_readability_accepts_x_range(tmp_path):
    body = tmp_path / "body.png"
    _png(body, 244)
    receipt = x_repair._body_media_readability([body])
    assert receipt["status"] == "PASS"
    assert receipt["images"][0]["height"] == 244


def test_source_media_readability_uses_projected_x_height(tmp_path):
    narrow = tmp_path / "narrow.png"
    _png(narrow, 650, width=200)
    receipt = x_repair._body_media_readability([narrow])
    assert receipt["status"] == "FAIL"
    assert any("too-tall" in item for item in receipt["violations"])


def test_publish_path_persists_receipt_and_quarantines_readability_failure():
    source = MODULE.read_text(encoding="utf-8")
    assert '"media-readability.json"' in source
    assert '_guard("mark-unavailable", pair, reason=reason)' in source
    assert '"action": "quarantined-unreadable-media"' in source


def test_inplace_repair_uses_repository_chunk_inserter_only():
    source = MODULE.read_text(encoding="utf-8")
    method = inspect.getsource(x_repair.XBrowserAdapter.replace_and_publish)
    assert "from x_anchor import build_chunks" in source
    assert "chunks = build_chunks(body_html, content_images)" in method
    assert "self._paste_image_chunk(page, composer, path)" in method
    assert method.index("chunks = build_chunks") < method.index("manager, _browser, page = self._page()")
    assert ".claude/skills/x-article-publisher" not in source
    assert "spec_from_file_location" not in source
