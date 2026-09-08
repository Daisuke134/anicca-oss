import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/writer-agent/scripts/note-publish/rebuild-note-body.py"


def test_rebuild_reaches_draft_update_with_explicit_portable_inputs(tmp_path: Path) -> None:
    package = tmp_path / "fake-note-mcp/note_mcp/api"
    package.mkdir(parents=True)
    for init in (package.parent / "__init__.py", package / "__init__.py"):
        init.write_text("", encoding="utf-8")
    (package.parent / "models.py").write_text(
        "class Session:\n"
        " def __init__(self, **values): self.values = values\n"
        "class ArticleInput:\n"
        " def __init__(self, **values): self.values = values\n",
        encoding="utf-8",
    )
    (package / "articles.py").write_text(
        "import json, os\n"
        "async def update_article(session, article_id, article):\n"
        " open(os.environ['REBUILD_RECEIPT'], 'w').write(json.dumps({'article_id': article_id, 'title': article.values['title'], 'tags': article.values['tags']}))\n"
        "def generate_image_html(url, **values): return url\n",
        encoding="utf-8",
    )
    (package / "images.py").write_text(
        "async def upload_body_image(*args): raise AssertionError('no images expected')\n",
        encoding="utf-8",
    )
    source = tmp_path / "article.md"
    source.write_text("# Portable article\n\nBody.\n", encoding="utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    state = tmp_path / "writer"
    work = state / "note-work"
    work.mkdir(parents=True)
    (work / "note-cookies.json").write_text("{}", encoding="utf-8")
    receipt = tmp_path / "receipt.json"

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "NOTE_MCP_SRC": str(package.parents[1]),
            "NOTE_SRC": str(source),
            "NOTE_NUM": "portable-article-id",
            "NOTE_ASSETS": str(assets),
            "NOTE_USER_ID": "configured-user-id",
            "NOTE_URLNAME": "configured-urlname",
            "WRITER_STATE_DIR": str(state),
            "REBUILD_RECEIPT": str(receipt),
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "article_id": "portable-article-id",
        "title": "Portable article",
        "tags": [],
    }
