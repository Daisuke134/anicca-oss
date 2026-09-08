from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "note-publish" / "set-eyecatch-draft.py"


def test_existing_authenticated_eyecatch_is_reused_without_upload(tmp_path: Path) -> None:
    """A resume must not require the upload button after the cover already landed."""
    home = tmp_path / "home"
    work = home / ".local/state/life-manager/writer/note-work"
    work.mkdir(parents=True)
    (work / "thumb.png").write_bytes(b"immutable-cover")
    (work / "note-cookies.json").write_text('{"session":"test"}\n')
    calls = tmp_path / "calls"
    fake_module = tmp_path / "cloakbrowser.py"
    fake_module.write_text(
        """
import os

EXISTING = "https://assets.st-note.com/production/uploads/images/existing.png"

class Page:
    def set_viewport_size(self, value):
        pass
    def goto(self, *args, **kwargs):
        pass
    def evaluate(self, source):
        if "document.body.innerText" in source:
            return "公開に進む"
        if "img[alt=\\\"eyecatch\\\"]" in source:
            return EXISTING
        return None
    def click(self, selector):
        with open(os.environ["CALLS"], "a") as stream:
            stream.write(selector + "\\n")
        if selector == 'button[aria-label="画像を追加"]':
            raise RuntimeError("upload button is absent after an eyecatch exists")
    def screenshot(self, path):
        with open(path, "wb") as stream:
            stream.write(b"png")

class Context:
    def add_cookies(self, value):
        pass
    def new_page(self):
        return Page()
    def close(self):
        pass

def launch_context(**kwargs):
    return Context()
""".lstrip()
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "NOTE_KEY": "n-existing",
        "CALLS": str(calls),
        "PYTHONPATH": str(tmp_path),
        "WRITER_STATE_DIR": str(home / ".local/state/life-manager/writer"),
    }

    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (
        "EYECATCH_IN_EDITOR: "
        "https://assets.st-note.com/production/uploads/images/existing.png"
        in completed.stdout
    )
    assert not calls.exists() or 'button[aria-label="画像を追加"]' not in calls.read_text()
