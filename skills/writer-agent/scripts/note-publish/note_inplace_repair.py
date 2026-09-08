#!/usr/bin/env python3
"""Install immutable media on one protected live note key, never a new note."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
# --- fail-closed PII gate wiring ---------------------------------------------------
# gate_run_dir raises SystemExit (non-zero) on a finding, a missing blocklist, an unreadable
# artifact, or ANY scanner error. No code path here turns a failure into a publish.
import sys as _pii_sys  # noqa: E402
from pathlib import Path as _PiiPath  # noqa: E402
_pii_sys.path.insert(0, str(next(
    _p / "_shared"
    for _p in _PiiPath(__file__).resolve().parents
    if (_p / "_shared" / "pii_gate.py").is_file()
)))
from pii_gate import gate_run_dir  # noqa: E402

from typing import Any
from urllib.parse import urlparse


class NoteRepairRefused(RuntimeError):
    pass


SCRIPTS = Path(__file__).resolve().parents[1]


def png_dimensions(path: Path) -> tuple[int, int]:
    header = Path(path).read_bytes()[:24]
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        raise NoteRepairRefused("note media is not a readable PNG")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise NoteRepairRefused("note media has invalid dimensions")
    return width, height


def browser_eyecatch(path: Path, target: str) -> str:
    """Use the proven note editor flow when its upload API omits a URL."""
    work = Path(os.environ.get("HOME", str(Path.home()))) / ".cloak/note-work"
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, work / "thumb.png")
    helper = Path(__file__).resolve().parent / "set-eyecatch-draft.py"
    result = subprocess.run(
        [
            os.environ.get(
                "WRITER_CLOAK_PYTHON",
                os.environ.get("WRITER_BROWSER_PYTHON", sys.executable),
            ),
            str(helper),
        ],
        env={**os.environ, "NOTE_KEY": target},
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(
        r"^EYECATCH_IN_EDITOR:\s*"
        r"(https://assets\.st-note\.com/\S+)\s*$",
        result.stdout,
        re.MULTILINE,
    )
    if result.returncode != 0 or match is None:
        raise NoteRepairRefused(
            result.stderr.strip()
            or "note editor eyecatch fallback did not verify an asset"
        )
    return match.group(1)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _state() -> tuple[dict[str, Any], Path]:
    state_path = Path(os.environ.get("ARTICLE_PUBLICATION_STATE", ""))
    if not state_path.is_file():
        raise NoteRepairRefused("ARTICLE_PUBLICATION_STATE is required")
    value = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise NoteRepairRefused("publication state is malformed")
    return value, state_path


def _guard(command: str, pair: str) -> dict[str, Any]:
    state, _ = _state()
    target = str(
        state.get("pairs", {}).get(pair, {}).get("target", "")
    )
    arguments = [
        os.environ.get(
            "WRITER_SYSTEM_PYTHON",
            os.environ.get("WRITER_BROWSER_PYTHON", sys.executable),
        ),
        str(SCRIPTS / "publication-guard.py"),
        command,
        "--pair",
        pair,
    ]
    if command == "preflight":
        arguments.extend(
            ["--target-kind", "note-key", "--target", target]
        )
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise NoteRepairRefused(
            result.stderr.strip() or "note publication guard refused"
        )
    return json.loads(result.stdout)


def install_body_media(
    existing_html: str,
    image_html: str,
    marker: str,
) -> str:
    if marker in existing_html:
        return existing_html
    return f"{existing_html.rstrip()}\n{image_html}\n{marker}\n"


class NoteMcpAdapter:
    def __init__(self) -> None:
        note_mcp = Path(
            os.environ.get(
                "NOTE_MCP_SRC",
                str(SCRIPTS.parent / "vendor/note-mcp/src"),
            )
        )
        if str(note_mcp) not in sys.path:
            sys.path.insert(0, str(note_mcp))
        from note_mcp.api.articles import (  # pylint: disable=import-outside-toplevel
            generate_image_html,
            get_article_raw_html,
            publish_article,
            update_article_raw_html,
        )
        from note_mcp.api.images import (  # pylint: disable=import-outside-toplevel
            upload_body_image,
            upload_eyecatch_image,
        )
        from note_mcp.models import Session  # pylint: disable=import-outside-toplevel
        from note_mcp.auth.browser import get_current_user  # pylint: disable=import-outside-toplevel

        cookie_path = Path.home() / ".cloak/note-work/note-cookies.json"
        cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
        if not isinstance(cookies, dict) or not cookies:
            raise NoteRepairRefused("note cookie cache is empty")
        self.session = Session(
            cookies=cookies,
            user_id=os.environ["NOTE_USER_ID"],
            username=os.environ["NOTE_URLNAME"],
            created_at=int(time.time()),
        )
        self._get = get_article_raw_html
        self._publish = publish_article
        self._update = update_article_raw_html
        self._upload_body = upload_body_image
        self._upload_eyecatch = upload_eyecatch_image
        self._image_html = generate_image_html
        self._get_current_user = get_current_user

    async def identity(self) -> str:
        actual = await self._get_current_user(self.session.cookies)
        actual_id = str(actual.get("id", ""))
        actual_urlname = str(actual.get("urlname", "")).strip().lower()
        if (
            actual_id != os.environ["NOTE_USER_ID"]
            or actual_urlname != os.environ["NOTE_URLNAME"].strip().lower()
        ):
            raise NoteRepairRefused(
                "authenticated Note account does not match NOTE_USER_ID / NOTE_URLNAME"
            )
        return actual_urlname

    async def fetch(self, target: str) -> dict[str, Any]:
        article = await self._get(self.session, target)
        return {
            "public_id": str(article.key),
            "title": str(article.title),
            "html": str(article.body),
            "tags": list(article.tags),
            "status": str(article.status.value),
        }

    async def upload_body(self, path: str, target: str) -> str:
        image = await self._upload_body(self.session, path, target)
        return str(image.url)

    async def image_html(self, url: str, path: str) -> str:
        width, height = png_dimensions(Path(path))
        display_width = min(width, 560)
        display_height = max(1, round(height * display_width / width))
        return str(
            self._image_html(
                url,
                width=display_width,
                height=display_height,
            )
        )

    async def update_raw(
        self,
        target: str,
        title: str,
        html: str,
        tags: list[str],
    ) -> str:
        article = await self._update(
            self.session,
            target,
            title,
            html,
            tags,
        )
        return str(article.key or target)

    async def upload_eyecatch(self, path: str, target: str) -> str:
        # The ambiguity recovery that authorizes this repair already proved
        # the public eyecatch against the immutable headline. Re-read that
        # dedicated editor slot first; the browser helper uploads only when it
        # is genuinely absent. Calling the API first would replace a valid
        # cover on every body-only repair.
        return browser_eyecatch(Path(path), target)

    async def publish(self, target: str, tags: list[str]) -> str:
        article = await self._publish(
            self.session,
            article_id=target,
            tags=tags,
        )
        return str(article.key)


def _journal(
    path: Path,
    *,
    run_id: str,
    target: str,
    media_hashes: dict[str, Any],
) -> dict[str, Any]:
    expected = {
        "version": 1,
        "run_id": run_id,
        "pair": "note/ja",
        "target": target,
        "media_sha256": media_hashes,
    }
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, json.JSONDecodeError):
        current = dict(expected)
    for key, value in expected.items():
        if current.get(key) != value:
            raise NoteRepairRefused(
                "note repair journal conflicts with immutable run"
            )
    return current


async def _repair(adapter: Any) -> dict[str, Any]:
    state, state_path = _state()
    pair = "note/ja"
    entry = state.get("pairs", {}).get(pair, {})
    protected = entry.get("existing_publication", {})
    target = str(entry.get("target", ""))
    live_url = str(protected.get("live_url", ""))
    parsed = urlparse(live_url)
    if (
        str(protected.get("public_id", "")) != target
        or parsed.scheme != "https"
        or parsed.hostname != "note.com"
        or parsed.path != f"/{state.get('destination_identities', {}).get(pair, '')}/n/{target}"
    ):
        raise NoteRepairRefused(
            "note repair target is not the protected live key"
        )
    expected_identity = (
        state.get("destination_identities", {}).get(pair)
    )
    if await adapter.identity() != expected_identity:
        raise NoteRepairRefused(
            "authenticated note identity does not match protected account"
        )
    decision = _guard("preflight", pair)
    if decision.get("action") == "skip-live":
        return decision
    if decision.get("action") != "repair-live":
        raise NoteRepairRefused("note in-place repair was not authorized")
    existing = await adapter.fetch(target)
    if (
        existing.get("public_id") != target
        or existing.get("status") != "published"
        or not existing.get("title")
        or not isinstance(existing.get("html"), str)
    ):
        raise NoteRepairRefused(
            "protected note key is not a readable published article"
        )
    media = state.get("media", {})
    headline = media.get("headline_image", {})
    body_assets = [
        item
        for item in media.get("body_assets", [])
        if isinstance(item, dict)
    ]
    if len(body_assets) != 1:
        raise NoteRepairRefused(
            "current note repair requires exactly one body diagram"
        )
    for item in [headline, *body_assets]:
        path = Path(str(item.get("path", "")))
        if not path.is_file() or sha256(path) != item.get("sha256"):
            raise NoteRepairRefused(
                "immutable note media is missing or changed"
            )
    journal_path = state_path.parent / "note-inplace-repair.json"
    media_hashes = {
        "headline": headline["sha256"],
        "body_assets": [item["sha256"] for item in body_assets],
    }
    journal = _journal(
        journal_path,
        run_id=str(state["run_id"]),
        target=target,
        media_hashes=media_hashes,
    )
    body_path = str(body_assets[0]["path"])
    if not journal.get("body_url"):
        journal["body_url"] = await adapter.upload_body(
            body_path, target
        )
        _atomic_json(journal_path, journal)
    marker = (
        f"<!-- article-run:{state['run_id']} "
        f"media:{body_assets[0]['sha256']} -->"
    )
    if not journal.get("body_saved"):
        image_html = await adapter.image_html(
            str(journal["body_url"]), body_path
        )
        updated_body = install_body_media(
            str(existing["html"]), image_html, marker
        )
        updated_id = await adapter.update_raw(
            target,
            str(existing["title"]),
            updated_body,
            list(existing.get("tags") or []),
        )
        if updated_id != target:
            raise NoteRepairRefused(
                "note body update changed the protected key"
            )
        journal["body_saved"] = True
        _atomic_json(journal_path, journal)
    if not journal.get("eyecatch_uploaded"):
        journal["eyecatch_url"] = await adapter.upload_eyecatch(
            str(headline["path"]), target
        )
        journal["eyecatch_uploaded"] = True
        _atomic_json(journal_path, journal)
    if not journal.get("republished"):
        published_id = await adapter.publish(
            target, list(existing.get("tags") or [])
        )
        if published_id != target:
            raise NoteRepairRefused(
                "note republish changed the protected key"
            )
        journal["republished"] = True
        _atomic_json(journal_path, journal)
    return _guard("reconcile", pair)


def repair(adapter: Any | None = None) -> dict[str, Any]:
    return asyncio.run(_repair(adapter or NoteMcpAdapter()))


def main() -> int:
    # Fail-closed PII gate: this repairs/republishes a LIVE article, so re-scan the frozen
    # artifacts before touching it. An unset ARTICLE_RUN_DIR is itself a refusal.
    gate_run_dir("note-inplace", os.environ.get("ARTICLE_RUN_DIR", ""), pair="note/ja")
    print(
        json.dumps(
            repair(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        NoteRepairRefused,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        raise SystemExit(2)
