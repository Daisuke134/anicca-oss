import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PARSER = ROOT / "skills/writer-agent/scripts/x-publish/parse_markdown.py"
SPEC = importlib.util.spec_from_file_location("writer_x_markdown_parser", PARSER)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_repository_parser_preserves_cover_body_images_and_dividers(tmp_path: Path) -> None:
    cover = tmp_path / "cover.png"
    body = tmp_path / "body.png"
    cover.write_bytes(b"cover")
    body.write_bytes(b"body")
    source = tmp_path / "article.md"
    source.write_text(
        "---\ntitle: ignored frontmatter\n---\n"
        "# Portable title\n\n"
        "![cover](cover.png)\n\n"
        "First paragraph.\n\n"
        "![figure](body.png)\n\n"
        "---\n\n"
        "## Section\n\nBody text.\n",
        encoding="utf-8",
    )

    parsed = MODULE.parse_markdown_file(str(source))

    assert parsed["title"] == "Portable title"
    assert parsed["cover_image"] == str(cover)
    assert parsed["cover_exists"] is True
    assert [item["path"] for item in parsed["content_images"]] == [str(body)]
    assert parsed["content_images"][0]["after_text"] == "First paragraph."
    assert len(parsed["dividers"]) == 1
    assert "<h2>Section</h2>" in parsed["html"]
    assert parsed["missing_images"] == 0


def test_missing_image_never_falls_back_to_unrelated_home_file(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True)
    (home / "Downloads/cover.png").write_bytes(b"unrelated")
    monkeypatch.setenv("HOME", str(home))
    source = tmp_path / "source/article.md"
    source.parent.mkdir()
    source.write_text("# Title\n\n![cover](cover.png)\n", encoding="utf-8")

    parsed = MODULE.parse_markdown_file(str(source))

    assert parsed["cover_image"] == str(source.parent / "cover.png")
    assert parsed["cover_exists"] is False
    assert parsed["missing_images"] == 1
