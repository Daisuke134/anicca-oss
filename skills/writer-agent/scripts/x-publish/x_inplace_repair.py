#!/usr/bin/env python3
"""Replace media on one saved X Article edit ID and publish that same ID."""

from __future__ import annotations

import argparse
import hashlib
import html as html_lib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
# --- fail-closed PII gate wiring ---------------------------------------------------
# gate_* raise SystemExit (non-zero) on a finding, a missing blocklist, an unreadable
# artifact, or ANY scanner error. There is no code path where a failure means publish.
import sys as _pii_sys  # noqa: E402
from pathlib import Path as _PiiPath  # noqa: E402
_pii_sys.path.insert(0, str(next(
    _p / "_shared"
    for _p in _PiiPath(__file__).resolve().parents
    if (_p / "_shared" / "pii_gate.py").is_file()
)))
from pii_gate import gate_files, gate_run_dir, gate_text  # noqa: E402,F401
from browser_clipboard import browser_write_html, browser_write_image  # noqa: E402

from typing import Any
from urllib.parse import urlparse


class XRepairRefused(RuntimeError):
    pass


SCRIPTS = Path(__file__).resolve().parents[1]
EDIT_RE = re.compile(
    r"^https://x\.com/compose/articles/edit/([0-9]{8,})$"
)
CANONICAL_MEDIA_START = "<!-- canonical-media:start -->"
CANONICAL_MEDIA_END = "<!-- canonical-media:end -->"
X_RENDER_WIDTH = 587
X_BODY_MIN_HEIGHT = 110
X_BODY_MAX_HEIGHT = 650


def _normalized_title(value: str) -> str:
    return " ".join(value.split())


def _source_title(path: Path) -> str:
    source = Path(path).read_text(encoding="utf-8")
    frontmatter = re.match(
        r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)",
        source,
        re.S,
    )
    if frontmatter is not None:
        for line in frontmatter.group(1).splitlines():
            if line.lower().startswith("title:"):
                title = line.split(":", 1)[1].strip().strip("\"'")
                if title:
                    return title
    heading = re.search(r"(?m)^#{1,6}\s+(.+?)\s*$", source)
    if heading is None or not heading.group(1).strip():
        raise XRepairRefused(
            "immutable X Article artifact has no title"
        )
    return heading.group(1).strip()


def _public_id_from_url(value: str, identity: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "x.com":
        raise XRepairRefused("X public Article URL is not canonical")
    match = re.fullmatch(
        rf"/{re.escape(identity)}/(?:article|status)/([0-9]{{8,}})",
        parsed.path.rstrip("/"),
    )
    if match is None:
        raise XRepairRefused("X public Article identity is unreadable")
    return match.group(1)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _body_media_readability(paths: list[Path]) -> dict[str, Any]:
    """Measure immutable body media before any X editor mutation."""
    try:
        from PIL import Image
    except ImportError as error:
        raise XRepairRefused("X media readability decoder is unavailable") from error
    images: list[dict[str, Any]] = []
    violations: list[str] = []
    for path in paths:
        if not path.is_file():
            violations.append(f"missing:{path}")
            continue
        try:
            with Image.open(path) as image:
                width, height = image.size
        except Exception as error:
            violations.append(f"decode:{path}:{error}")
            continue
        row = {
            "path": str(path),
            "sha256": sha256(path),
            "width": int(width),
            "height": int(height),
        }
        images.append(row)
        projected_height = round(X_RENDER_WIDTH * height / width, 2) if width else 0
        row["projected_height"] = projected_height
        if projected_height < X_BODY_MIN_HEIGHT:
            violations.append(
                f"too-flat:{path}:source={width}x{height}:projected={projected_height}:min={X_BODY_MIN_HEIGHT}"
            )
        elif projected_height > X_BODY_MAX_HEIGHT:
            violations.append(
                f"too-tall:{path}:source={width}x{height}:projected={projected_height}:max={X_BODY_MAX_HEIGHT}"
            )
    return {
        "version": 1,
        "status": "PASS" if not violations else "FAIL",
        "min_height": X_BODY_MIN_HEIGHT,
        "max_height": X_BODY_MAX_HEIGHT,
        "render_width": X_RENDER_WIDTH,
        "images": images,
        "violations": violations,
    }


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
    path = Path(os.environ.get("ARTICLE_PUBLICATION_STATE", ""))
    if not path.is_file():
        raise XRepairRefused("ARTICLE_PUBLICATION_STATE is required")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise XRepairRefused("publication state is malformed")
    return value, path


def _guard(
    command: str,
    pair: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    state, _ = _state()
    target = str(state.get("pairs", {}).get(pair, {}).get("target", ""))
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
            ["--target-kind", "x-draft-url", "--target", target]
        )
    if command in {"quarantine-missing-media", "mark-unavailable"}:
        if not reason:
            raise XRepairRefused("unavailable reason is required")
        arguments.extend(["--reason", reason])
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise XRepairRefused(
            result.stderr.strip() or "X publication guard refused"
        )
    return json.loads(result.stdout)


def _immutable_path(descriptor: dict[str, Any], label: str) -> Path:
    path = Path(str(descriptor.get("path", "")))
    if not path.is_file() or sha256(path) != descriptor.get("sha256"):
        raise XRepairRefused(f"immutable {label} is missing or changed")
    return path


def _adapt_source(
    state: dict[str, Any],
    pair: str,
    source: Path,
    body_assets: list[dict[str, Any]],
    destination: Path,
) -> Path:
    language = pair.rsplit("/", 1)[1]
    marker = (
        f"<!-- article-run:{state['run_id']} "
        f"x-media:{','.join(str(item['sha256']) for item in body_assets)} -->"
    )
    body = source.read_text(encoding="utf-8").rstrip()
    marker_counts = (
        body.count(CANONICAL_MEDIA_START),
        body.count(CANONICAL_MEDIA_END),
    )
    if marker_counts not in {(0, 0), (1, 1)}:
        raise XRepairRefused("canonical media envelope is malformed")
    media = ""
    if marker_counts == (0, 0):
        media = "\n\n".join(
            f"![Explanatory diagram {index}]({item['path']})"
            for index, item in enumerate(body_assets, 1)
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    media_suffix = f"\n\n{media}" if media else ""
    destination.write_text(
        f"{body}{media_suffix}\n\n{marker}\n",
        encoding="utf-8",
    )
    if language not in {"ja", "en"}:
        raise XRepairRefused("invalid X Article language")
    return destination


class XBrowserAdapter:
    """Authenticated daily-driver adapter; it never opens a new-article URL."""

    def __init__(self) -> None:
        self.cdp = os.environ.get(
            "WRITER_CDP_URL", "http://localhost:9222"
        )
        self.clipboard = (
            Path(__file__).resolve().parent / "browser_clipboard.py"
        )

    def _page(self):
        from playwright.sync_api import sync_playwright

        manager = sync_playwright().start()
        browser = manager.chromium.connect_over_cdp(self.cdp)
        if not browser.contexts:
            manager.stop()
            raise XRepairRefused("X daily-driver has no browser context")
        page = browser.contexts[0].new_page()
        return manager, browser, page

    @staticmethod
    def _reacquire_saved_editor(browser: Any, target: str) -> Any:
        """Recover X's replacement tab without crossing the fixed edit ID."""
        candidates = [
            candidate
            for context in browser.contexts
            for candidate in context.pages
            if not candidate.is_closed()
            and candidate.url.rstrip("/") == target.rstrip("/")
        ]
        if not candidates:
            raise XRepairRefused(
                "X replaced the editor tab but the fixed edit URL is absent"
            )
        page = candidates[-1]
        page.wait_for_timeout(1_000)
        return page

    def authenticated_identity(self) -> str:
        manager, _browser, page = self._page()
        try:
            page.goto(
                "https://x.com/home",
                wait_until="domcontentloaded",
                timeout=50_000,
            )
            page.wait_for_timeout(4_000)
            profile = page.locator(
                '[data-testid="AppTabBar_Profile_Link"]'
            )
            href = profile.first.get_attribute("href") if profile.count() else ""
            identity = str(href or "").strip("/")
            if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", identity):
                raise XRepairRefused(
                    "authenticated X identity is unreadable"
                )
            return identity
        finally:
            page.close()
            manager.stop()

    @staticmethod
    def _article_card(page, target: str, tab: str):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                page.goto(
                    "https://x.com/compose/articles",
                    wait_until="domcontentloaded",
                    timeout=50_000,
                )
                page.wait_for_timeout(4_000)
                selected = page.get_by_role(
                    "tab", name=tab, exact=True
                )
                selected.first.wait_for(
                    state="visible", timeout=15_000
                )
                if selected.count() != 1:
                    raise XRepairRefused(
                        f"X {tab} tab is ambiguous"
                    )
                selected.click()
                path = urlparse(target).path
                card = page.locator(f'a[href="{path}"]')
                card.first.wait_for(
                    state="visible", timeout=15_000
                )
                if card.count() != 1:
                    raise XRepairRefused(
                        f"X {tab} dashboard does not contain "
                        "the saved edit ID"
                    )
                return card.first
            except Exception as error:
                last_error = error
                if attempt < 2:
                    page.wait_for_timeout(1_000)
        raise XRepairRefused(
            f"X {tab} dashboard remained unavailable after retries"
        ) from last_error

    @staticmethod
    def _card_title(card) -> str:
        candidates = card.locator("span").evaluate_all(
            """(nodes) => nodes
                .map((node) => node.innerText.trim())
                .filter((text) =>
                    text.length >= 20 &&
                    text.length <= 300 &&
                    !text.includes("\\n")
                )"""
        )
        if not candidates:
            raise XRepairRefused("X dashboard title is unreadable")
        return str(candidates[0]).strip()

    def _published_mapping(
        self,
        page,
        target: str,
        identity: str,
    ) -> dict[str, str]:
        card = self._article_card(page, target, "Published")
        edit_match = EDIT_RE.fullmatch(target)
        if edit_match is None:
            raise XRepairRefused("X published mapping lost the edit ID")
        more = card.get_by_role("button", name="More", exact=True)
        if more.count() != 1:
            raise XRepairRefused("X published Article menu is ambiguous")
        more.click()
        page.wait_for_timeout(400)
        view = page.get_by_text("View Article", exact=True)
        if view.count() != 1:
            raise XRepairRefused("X View Article action is ambiguous")
        view.click()
        page.wait_for_timeout(2_500)
        return {
            "edit_id": edit_match.group(1),
            "public_id": _public_id_from_url(page.url, identity),
            "public_url": page.url,
        }

    def current_title(
        self,
        target: str,
        protected: dict[str, Any] | None = None,
    ) -> str:
        manager, _browser, page = self._page()
        try:
            page.goto(
                target,
                wait_until="domcontentloaded",
                timeout=50_000,
            )
            page.wait_for_timeout(5_000)
            title = page.locator('textarea[placeholder="Add a title"]')
            if title.count() == 1:
                value = title.input_value().strip()
                if not value:
                    raise XRepairRefused("saved X Article title is empty")
                return value
            # The editor URL renders the Articles list, so the title
            # textarea is absent for an already-published article. The
            # Published tab card is keyed by the same edit target, so it
            # resolves the title without needing a prior recorded
            # publication — which could never exist before the first receipt
            # (measured 2026-07-26: three published JA articles were stuck in
            # exactly that circle).
            card = self._article_card(page, target, "Published")
            return self._card_title(card)
        finally:
            page.close()
            manager.stop()

    def ensure_editable(
        self,
        target: str,
        title: str,
        protected: dict[str, Any],
    ) -> dict[str, Any]:
        edit_match = EDIT_RE.fullmatch(target)
        if edit_match is None:
            raise XRepairRefused("X unpublish requires an exact edit ID")
        live_url = str(protected.get("live_url", ""))
        live_path = urlparse(live_url).path.strip("/").split("/")
        if len(live_path) != 3:
            raise XRepairRefused("protected X live URL is malformed")
        identity = live_path[0]
        public_id = str(protected.get("public_id", ""))
        manager, _browser, page = self._page()
        try:
            page.goto(
                target,
                wait_until="domcontentloaded",
                timeout=50_000,
            )
            page.wait_for_timeout(4_000)
            title_field = page.locator(
                'textarea[placeholder="Add a title"]'
            )
            composer = page.locator('[data-testid="composer"]')
            if title_field.count() == 1 and composer.count() == 1:
                if _normalized_title(title_field.input_value()) != (
                    _normalized_title(title)
                ):
                    raise XRepairRefused(
                        "unpublished X editor title changed"
                    )
                return {
                    "edit_id": edit_match.group(1),
                    "public_id": public_id,
                    "was_unpublished": False,
                }

            card = self._article_card(page, target, "Published")
            if _normalized_title(self._card_title(card)) != (
                _normalized_title(title)
            ):
                raise XRepairRefused(
                    "published X dashboard title changed"
                )
            mapping = self._published_mapping(page, target, identity)
            if mapping["public_id"] != public_id:
                raise XRepairRefused(
                    "published X dashboard maps to a different public ID"
                )

            card = self._article_card(page, target, "Published")
            more = card.get_by_role("button", name="More", exact=True)
            if more.count() != 1:
                raise XRepairRefused("X published Article menu is ambiguous")
            more.click()
            page.wait_for_timeout(400)
            action = page.get_by_text(
                "Unpublish, move Article to drafts", exact=True
            )
            if action.count() != 1:
                raise XRepairRefused("X unpublish action is ambiguous")
            action.click()
            page.wait_for_timeout(400)
            alert = page.locator('[role="alertdialog"]')
            if alert.count() != 1:
                raise XRepairRefused("X unpublish confirmation is missing")
            confirm = alert.get_by_role(
                "button", name="Unpublish, move to drafts", exact=True
            )
            if confirm.count() != 1:
                raise XRepairRefused("X unpublish confirmation is ambiguous")
            confirm.click()
            page.wait_for_timeout(3_000)
            page.goto(
                target,
                wait_until="domcontentloaded",
                timeout=50_000,
            )
            page.wait_for_timeout(4_000)
            title_field = page.locator(
                'textarea[placeholder="Add a title"]'
            )
            composer = page.locator('[data-testid="composer"]')
            if (
                title_field.count() != 1
                or composer.count() != 1
                or _normalized_title(title_field.input_value())
                != _normalized_title(title)
            ):
                raise XRepairRefused(
                    "X Article did not become the same editable draft"
                )
            return {
                **mapping,
                "was_unpublished": True,
            }
        finally:
            page.close()
            manager.stop()

    def _prepare(
        self,
        title: str,
        source: str,
        cover: str,
    ) -> dict[str, Any]:
        work = Path(source).parent / "prepared"
        work.mkdir(parents=True, exist_ok=True)
        prepared = work / "article-x.md"
        assets = work / "assets"
        assets.mkdir(exist_ok=True)
        environment = {
            **os.environ,
            "X_SRC": source,
            "X_DST": str(prepared),
            "X_ASSETS": str(assets),
            "X_TITLE": title,
            "X_COVER": cover,
        }
        venv_python = Path(
            os.environ.get(
                "WRITER_CLOAK_PYTHON",
                os.environ.get("WRITER_BROWSER_PYTHON", sys.executable),
            )
        )
        prep = subprocess.run(
            [
                str(venv_python),
                str(Path(__file__).resolve().parent / "prep-x-md.py"),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if prep.returncode != 0:
            raise XRepairRefused(
                prep.stderr.strip() or "X Markdown preparation failed"
            )
        parser = Path(
            os.environ.get(
                "WRITER_X_MARKDOWN_PARSER",
                str(
                    Path.home()
                    / ".claude/skills/x-article-publisher/scripts/"
                    "parse_markdown.py"
                ),
            )
        )
        parsed = subprocess.run(
            [
                os.environ.get(
                    "WRITER_SYSTEM_PYTHON",
                    os.environ.get("WRITER_BROWSER_PYTHON", sys.executable),
                ),
                str(parser),
                str(prepared),
                "--output",
                "json",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if parsed.returncode != 0:
            raise XRepairRefused(
                parsed.stderr.strip() or "X Markdown parsing failed"
            )
        value = json.loads(parsed.stdout)
        images = value.get("content_images", [])
        for image in images:
            if not isinstance(image, dict):
                continue
            recovered = self._html_suffix_anchor(
                str(value.get("html", "")), str(image.get("after_text", ""))
            )
            if recovered:
                image["after_text"] = recovered
        markdown_images = len(
            re.findall(r"^!\[", prepared.read_text(encoding="utf-8"), re.M)
        )
        if len(images) != markdown_images - 1:
            raise XRepairRefused(
                "X parser dropped a cover or body image"
            )
        return value

    def _clipboard_html(self, page, value: str) -> None:
        try:
            browser_write_html(page, value)
        except Exception as error:
            raise XRepairRefused(
                f"X HTML clipboard write failed: {error}"
            ) from error

    def _clipboard_image(self, page, path: str) -> None:
        try:
            browser_write_image(page, path)
        except Exception as error:
            raise XRepairRefused(
                f"X image clipboard write failed: {error}"
            ) from error

    def _paste_image_chunk(self, page, composer, path: Path) -> None:
        before = composer.locator("img").count()
        for attempt in range(3):
            self._clipboard_image(page, str(path))
            page.wait_for_timeout(400)
            page.keyboard.press("Meta+v")
            for _ in range(30):
                page.wait_for_timeout(500)
                if composer.locator("img").count() > before:
                    page.keyboard.press("ArrowDown")
                    page.wait_for_timeout(300)
                    return
            if attempt < 2:
                page.wait_for_timeout(1_000)
        raise XRepairRefused("X body image paste failed")

    @staticmethod
    def _rendered_anchor(anchor: str) -> str:
        # X/Draft.js renders a Markdown link label and the text after it as
        # separate DOM text nodes. Keeping both in one anchor therefore makes
        # the canonical inserter miss an otherwise visible paragraph (measured
        # on daily-2026-08-21: ``X long-form example — 長文``). Prefer the
        # post-link text when present; it is usually unique while remaining a
        # single stable text node. A link-only anchor falls back to its label.
        link = re.search(r"\[([^\]]+)\]\([^)]+\)", anchor)
        if link is not None and link.group(1).strip():
            suffix = anchor[link.end():].lstrip(" \t-—–:：")
            return suffix.strip() or link.group(1).strip()
        rendered = re.sub(
            r"^(?:#{1,6}|[-*+])\s+",
            "",
            anchor,
        )
        rendered = re.sub(
            r"\[([^\]]+)\]\([^)]+\)",
            r"\1",
            rendered,
        )
        # parse_markdown bounds after_text to 80 characters. A long URL can
        # therefore lose its closing ")" even though the reader-visible link
        # label is complete and present in the editor DOM.
        truncated_link = re.match(r"^\[([^\]]+)\]\(", rendered)
        if truncated_link is not None:
            return truncated_link.group(1)
        raw_url_suffix = re.match(
            r"^(?P<label>.+?)\s+https?://\S*$",
            rendered,
        )
        if raw_url_suffix is not None:
            return raw_url_suffix.group("label").rstrip()
        return rendered

    @staticmethod
    def _html_suffix_anchor(html: str, raw_anchor: str) -> str:
        """Recover a visible suffix when the parser clipped its context."""
        url = re.search(r"\]\((https?://[^)\s]+)", raw_anchor)
        if url is None:
            return ""
        for block in re.finditer(
            r"<(?:li|p|h[1-6]|blockquote)\b[^>]*>(.*?)</(?:li|p|h[1-6]|blockquote)>",
            html,
            re.IGNORECASE | re.DOTALL,
        ):
            fragment = block.group(0)
            if url.group(1) not in fragment or "</a>" not in fragment:
                continue
            suffix = fragment.split("</a>", 1)[1]
            visible = html_lib.unescape(re.sub(r"<[^>]+>", " ", suffix))
            return visible.strip().lstrip("-—–:： ").strip()
        return ""

    @staticmethod
    def _first_reader_visible_anchor(html: str) -> str:
        """Return a stable text anchor from the first non-empty body block."""
        def longest_segment(value: str) -> str:
            segments = []
            for raw in re.findall(r"[^<>]+", value):
                visible = html_lib.unescape(raw)
                visible = " ".join(visible.split())
                if visible:
                    segments.append(visible)
            return max(segments, key=len, default="")[:80]

        block_pattern = re.compile(
            r"<(?:p|h[1-6]|li|blockquote)\b[^>]*>(.*?)"
            r"</(?:p|h[1-6]|li|blockquote)>",
            re.IGNORECASE | re.DOTALL,
        )
        for match in block_pattern.finditer(html):
            visible = longest_segment(match.group(1))
            if visible:
                return visible[:80]
        return longest_segment(html)

    @staticmethod
    def _bind_leading_content_image_anchor(
        parsed: dict[str, Any],
        images: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Give a leading image the first reader-visible body anchor.

        The Markdown parser represents an image immediately after the title or
        cover with ``block_index=0`` and no preceding text. X's canonical
        inserter requires a non-empty anchor, so bind only that leading image
        to the first body block while preserving the parsed list and order.
        """
        bound = [dict(image) for image in images]
        if not bound:
            return bound
        first = bound[0]
        if int(first.get("block_index", 0)) != 0:
            return bound
        if str(first.get("after_text") or "").strip():
            return bound
        anchor = XBrowserAdapter._first_reader_visible_anchor(
            str(parsed.get("html", ""))
        )
        if not anchor:
            raise XRepairRefused(
                "X leading body image has no reader-visible anchor"
            )
        first["after_text"] = anchor
        return bound

    @staticmethod
    def _chunks(parsed: dict[str, Any]) -> list[tuple[str, str]]:
        html = str(parsed.get("html", ""))
        position = 0
        chunks: list[tuple[str, str]] = []
        previous_anchor = ""
        for image in sorted(
            parsed.get("content_images", []),
            key=lambda item: item["block_index"],
        ):
            anchor = str(image.get("after_text") or "")
            if anchor and anchor == previous_anchor:
                chunks.append(("image", str(image["path"])))
                continue
            rendered_anchor = XBrowserAdapter._rendered_anchor(anchor)
            rendered_link_anchor = rendered_anchor
            candidates = [anchor, rendered_anchor, rendered_link_anchor]
            candidates.extend(
                candidate[:20]
                for candidate in list(candidates)
                if len(candidate) > 20
            )
            index = next(
                (
                    found
                    for candidate in candidates
                    if candidate
                    and (found := html.find(candidate, position)) != -1
                ),
                -1,
            )
            if index == -1:
                # The live HTML normalises whitespace and entities, and an
                # anchor can legitimately sit before the running cursor when
                # the platform reorders blocks. Search the whole document on
                # a whitespace-collapsed copy before refusing (2026-07-26:
                # every anchor existed in the article, just not after the
                # cursor).
                flat = re.sub(r"\s+", " ", html)
                offsets: list[int] = []
                cursor = 0
                for character in html:
                    offsets.append(cursor)
                    cursor += 1
                for candidate in candidates:
                    if not candidate:
                        continue
                    flat_index = flat.find(re.sub(r"\s+", " ", candidate))
                    if flat_index != -1:
                        index = min(
                            len(html) - 1,
                            max(0, html.find(candidate.strip()[:12])),
                        )
                        break
            if index == -1:
                raise XRepairRefused(
                    "X body image anchor is missing from parsed HTML"
                )
            closing = re.search(
                r"</(?:p|h[1-6]|li|blockquote)>",
                html[index:],
                re.IGNORECASE,
            )
            end = (
                index + closing.end()
                if closing is not None
                else len(html)
            )
            chunks.extend(
                [
                    ("html", html[position:end]),
                    ("image", str(image["path"])),
                ]
            )
            position = end
            previous_anchor = anchor
        chunks.append(("html", html[position:]))
        return chunks

    @staticmethod
    def _unique_content_images(
        images: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        unique: list[dict[str, Any]] = []
        for image in images:
            key = (
                str(image.get("path", "")),
                str(image.get("after_text", "")),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(image)
        return unique

    @staticmethod
    def _body_probe_present(actual: str, probe: str) -> bool:
        compact_actual = re.sub(r"\s+", "", actual)
        compact_probe = re.sub(r"\s+", "", probe)
        return bool(compact_probe) and compact_probe in compact_actual

    def replace_and_publish(
        self,
        target: str,
        title: str,
        source: str,
        cover: str,
        protected: dict[str, Any] | None = None,
        readability_receipt: Path | None = None,
    ) -> dict[str, Any]:
        match = EDIT_RE.fullmatch(target)
        if not match:
            raise XRepairRefused("X mutation requires an exact saved edit URL")
        parsed = self._prepare(title, source, cover)
        content_images = self._unique_content_images(
            [
                dict(image)
                for image in parsed.get("content_images", [])
                if isinstance(image, dict)
            ]
        )
        content_images = self._bind_leading_content_image_anchor(
            parsed,
            content_images,
        )
        expected_images = len(content_images)
        inserted_image_sha256 = [
            sha256(Path(str(image["path"])))
            for image in content_images
        ]
        readability = _body_media_readability(
            [Path(str(image["path"])) for image in content_images]
        )
        if readability["status"] != "PASS":
            raise XRepairRefused(
                "X body media readability gate failed: "
                + "; ".join(str(item) for item in readability["violations"])
            )
        manager, _browser, page = self._page()
        try:
            page.goto(
                target,
                wait_until="domcontentloaded",
                timeout=50_000,
            )
            page.wait_for_timeout(5_000)
            if page.url.rstrip("/") != target.rstrip("/"):
                raise XRepairRefused(
                    "saved X edit URL redirected before mutation"
                )
            title_field = page.locator(
                'textarea[placeholder="Add a title"]'
            )
            composer = page.locator('[data-testid="composer"]')
            if (
                title_field.count() != 1
                or title_field.input_value().strip() != title
                or composer.count() != 1
            ):
                raise XRepairRefused(
                    "saved X editor identity changed before mutation"
                )
            composer.click()
            page.keyboard.press("Meta+A")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(500)
            if composer.inner_text().strip():
                raise XRepairRefused("X composer did not clear deterministically")
            body_html = str(parsed.get("html", ""))
            self._clipboard_html(page, body_html)
            page.keyboard.press("Meta+v")
            page.wait_for_timeout(3_000)
            normalized_body = " ".join(
                html_lib.unescape(re.sub(r"<[^>]+>", " ", body_html)).split()
            )
            actual_body = " ".join(composer.inner_text().split())
            probes = [normalized_body[:80], normalized_body[-80:]]
            if any(
                probe and not self._body_probe_present(actual_body, probe)
                for probe in probes
            ):
                raise XRepairRefused("X composer body text is incomplete")
            canonical_path = (
                Path.home()
                / ".claude/skills/x-article-publisher/scripts/publish_md_to_x.py"
            )
            canonical_spec = importlib.util.spec_from_file_location(
                "x_article_publisher", canonical_path
            )
            if canonical_spec is None or canonical_spec.loader is None:
                raise XRepairRefused("canonical X image inserter is unavailable")
            canonical = importlib.util.module_from_spec(canonical_spec)
            canonical_spec.loader.exec_module(canonical)
            # Reuse the canonical DOM anchor/search/postcondition code, but
            # replace only its OS-pasteboard side effect. The canonical module
            # imports an older helper that calls AppKit directly; launchd has
            # no reliable NSPasteboard server, while this authenticated X page
            # already has clipboard-write permission.
            canonical.copy_image_to_clipboard = (
                lambda image_path, quality=85: browser_write_image(
                    page, str(image_path)
                )
            )
            for image in sorted(
                content_images,
                key=lambda item: int(item.get("block_index", 0)),
            ):
                path = Path(str(image["path"]))
                if not path.is_file():
                    raise XRepairRefused("X body image is missing")
                anchor = self._rendered_anchor(
                    str(image.get("after_text", ""))
                )
                probe = canonical.search_phrase(anchor)
                matches = page.evaluate(
                    """(text) => {
                        const editor = document.querySelector('div[data-testid="composer"]')
                            || document.querySelector('div.public-DraftEditor-content');
                        if (!editor || !text) return 0;
                        const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
                        let node, count = 0;
                        while ((node = walker.nextNode())) {
                            if ((node.textContent || '').includes(text)) count += 1;
                        }
                        return count;
                    }""",
                    probe,
                )
                if matches != 1:
                    raise XRepairRefused(
                        f"X body image anchor is not unique: {probe!r} matches={matches}"
                    )
                if not canonical.insert_content_image(page, path, anchor):
                    raise XRepairRefused("X body image insertion failed")
            file_input = page.locator('input[type="file"]')
            if file_input.count() != 1 or not Path(cover).is_file():
                raise XRepairRefused("X cover input or immutable cover is missing")
            file_input.set_input_files(cover)
            page.wait_for_timeout(3_000)
            apply = page.get_by_text(
                re.compile(r"^(Apply|Save|適用|保存)$")
            )
            try:
                if apply.count():
                    apply.last.click()
                    page.wait_for_timeout(2_000)
                page.wait_for_timeout(5_000)
            except Exception as exc:
                if "closed" not in str(exc).lower():
                    raise
                # X may replace the editor target after applying a cover.
                # Rebind only to the exact persisted edit URL; never fall
                # through to a different draft or the new-article composer.
                page = self._reacquire_saved_editor(_browser, target)
                composer = page.locator('[data-testid="composer"]')
                page.wait_for_timeout(5_000)
            actual_images = composer.locator("img").count()
            if actual_images != expected_images:
                raise XRepairRefused(
                    f"X body image count is {actual_images}, "
                    f"expected {expected_images}"
                )
            rendered_sizes = page.evaluate(
                """()=>{const c=document.querySelector('[data-testid=composer]');
                    return c ? [...c.querySelectorAll('img')].map(im=>{const b=im.getBoundingClientRect();
                    return {width:Math.round(b.width),height:Math.round(b.height)}}) : [];}"""
            )
            rendered_violations = [
                f"rendered={row['width']}x{row['height']}"
                for row in rendered_sizes
                if row["height"] < X_BODY_MIN_HEIGHT
                or row["height"] > X_BODY_MAX_HEIGHT
            ]
            if rendered_violations:
                if readability_receipt is not None:
                    _atomic_json(
                        readability_receipt,
                        {
                            "version": 1,
                            "status": "FAIL",
                            "min_height": X_BODY_MIN_HEIGHT,
                            "max_height": X_BODY_MAX_HEIGHT,
                            "render_width": X_RENDER_WIDTH,
                            "images": rendered_sizes,
                            "violations": rendered_violations,
                            "stage": "native-editor",
                        },
                    )
                raise XRepairRefused(
                    "X rendered media readability gate failed: "
                    + "; ".join(rendered_violations)
                )
            publish = page.get_by_text(
                re.compile(r"^(Publish|Update|公開する|更新)$")
            )
            visible = [
                publish.nth(index)
                for index in range(publish.count())
                if publish.nth(index).is_visible()
            ]
            if not visible:
                raise XRepairRefused("X Publish/Update control is missing")
            visible[0].click()
            page.wait_for_timeout(2_000)
            dialog = page.locator('[role="dialog"]')
            scope = dialog if dialog.count() else page.locator("body")
            confirm = scope.get_by_text(
                re.compile(r"^(Publish|Update|公開する|更新)$")
            )
            confirm_visible = [
                confirm.nth(index)
                for index in range(confirm.count())
                if confirm.nth(index).is_visible()
            ]
            if not confirm_visible:
                raise XRepairRefused("X publish confirmation is missing")
            confirm_visible[-1].click()
            page.wait_for_timeout(8_000)
            identity = "diceai0"
            if isinstance(protected, dict):
                live_path = urlparse(
                    str(protected.get("live_url", ""))
                ).path.strip("/").split("/")
                if len(live_path) != 3:
                    raise XRepairRefused(
                        "protected X live URL is malformed"
                    )
                identity = live_path[0]
            mapping = self._published_mapping(
                page, target, identity
            )
            return {
                **mapping,
                "title": title,
                "body_image_count": actual_images,
                "inserted_image_sha256": inserted_image_sha256,
                "publish_confirmed": True,
            }
        finally:
            page.close()
            manager.stop()


def repair(
    pair: str,
    adapter: Any | None = None,
) -> dict[str, Any]:
    if pair not in {"x-article/ja", "x-article/en"}:
        raise XRepairRefused("invalid X Article repair pair")
    state, state_path = _state()
    entry = state.get("pairs", {}).get(pair, {})
    if entry.get("status") == "ambiguous":
        # A pair frozen ambiguous can never reach preflight (it refuses
        # every ambiguous pair), so a same-run repair could never write its
        # own unblocking evidence -- circular (measured live 2026-07-26:
        # daily-2026-07-26 x-article/ja). Ask the guard to resolve the
        # ambiguity from the authenticated destination first; it only ever
        # recovers the exact bounded shapes publication_resume already
        # authorizes, so this adds no new publishing authority.
        recovery = _guard("recover-ambiguous", pair)
        if recovery.get("status") == "unavailable":
            return {
                "action": "quarantined-unresolved",
                "pair": pair,
                "reason": str(recovery.get("error", "")),
            }
        state, state_path = _state()
        entry = state.get("pairs", {}).get(pair, {})
    target = str(entry.get("target", ""))
    match = EDIT_RE.fullmatch(target)
    if not match:
        raise XRepairRefused("X repair requires an exact saved edit URL")
    edit_id = match.group(1)
    protected = entry.get("existing_publication")
    public_id = ""
    if isinstance(protected, dict):
        live_url = str(protected.get("live_url", ""))
        public_id = str(protected.get("public_id", ""))
        expected_identity = (
            state.get("destination_identities", {}).get(pair) or "diceai0"
        )
        if (
            not public_id
            or _public_id_from_url(live_url, expected_identity) != public_id
        ):
            raise XRepairRefused(
                "X repair lost the protected public Article ID"
            )
    expected_identity = (
        state.get("destination_identities", {}).get(pair) or "diceai0"
    )
    browser = adapter or XBrowserAdapter()
    if browser.authenticated_identity() != expected_identity:
        raise XRepairRefused(
            "authenticated X identity does not match destination"
        )
    title = browser.current_title(
        target,
        protected if isinstance(protected, dict) else None,
    )
    language = pair.rsplit("/", 1)[1]
    source = _immutable_path(
        state.get("drafts", {}).get(language, {}),
        f"{language} draft",
    )
    if not isinstance(protected, dict):
        expected_title = _source_title(source)
        if _normalized_title(title) != _normalized_title(expected_title):
            raise XRepairRefused(
                "saved X editor title does not match immutable artifact"
            )
        title = expected_title
    media = state.get("media", {})
    headline = media.get("headline_image", {})
    body_assets = [
        item
        for item in media.get("body_assets", [])
        if isinstance(item, dict)
    ]
    if not body_assets:
        raise XRepairRefused("X repair requires body media")
    cover = _immutable_path(headline, "headline image")
    for index, item in enumerate(body_assets, 1):
        _immutable_path(item, f"body image {index}")
    decision = _guard("preflight", pair)
    if decision.get("action") == "skip-live":
        return decision
    expected_action = (
        "repair-live" if isinstance(protected, dict) else "publish"
    )
    if decision.get("action") != expected_action:
        raise XRepairRefused(
            f"X guard did not authorize {expected_action}"
        )
    remote_proof = decision.get("remote")
    if expected_action == "publish" and not (
        isinstance(remote_proof, dict)
        and remote_proof.get("status") == "not-live"
        and remote_proof.get("verified") is True
        and remote_proof.get("content_verified") is True
        and remote_proof.get("artifact_sha256") == sha256(source)
        and remote_proof.get("target") == target
        and remote_proof.get("destination_identity") == expected_identity
        and remote_proof.get("identity_verified") is True
        and remote_proof.get("identity_source") == "x-authenticated-edit-url"
    ):
        raise XRepairRefused("X draft target/content readback proof is incomplete")
    work = state_path.parent / "x-inplace-repair" / language
    readability = _body_media_readability(
        [Path(str(item["path"])) for item in body_assets]
    )
    readability.update(
        {
            "run_id": state["run_id"],
            "pair": pair,
            "target": target,
            "target_kind": "x-draft-url",
            "readback_status": "not-live" if decision.get("action") == "publish" else "protected-live",
            "readback_verified": True,
            "content_verified": (
                remote_proof.get("content_verified") is True
                if isinstance(remote_proof, dict)
                else False
            ),
            "artifact_sha256": (
                remote_proof.get("artifact_sha256")
                if isinstance(remote_proof, dict)
                else None
            ),
            "destination_identity": expected_identity,
            "identity_verified": True,
            "identity_source": "x-authenticated-edit-url",
        }
    )
    _atomic_json(work / "media-readability.json", readability)
    if readability["status"] != "PASS":
        if decision.get("action") != "publish":
            raise XRepairRefused(
                "X body media readability failed after a live/protected preflight"
            )
        reason = "x-article body media readability failed: " + "; ".join(
            str(item) for item in readability["violations"]
        )
        unavailable = _guard("mark-unavailable", pair, reason=reason)
        return {
            "action": "quarantined-unreadable-media",
            "pair": pair,
            "reason": reason,
            "media_receipt": str(work / "media-readability.json"),
            "state": unavailable,
        }
    adapted = _adapt_source(
        state,
        pair,
        source,
        body_assets,
        work / "article-with-media.md",
    )
    journal_path = work / "journal.json"
    journal = {
        "version": 1,
        "run_id": state["run_id"],
        "pair": pair,
        "target": target,
        "edit_id": edit_id,
        "public_id": public_id or None,
        "source_sha256": sha256(source),
        "adapted_sha256": sha256(adapted),
        "headline_sha256": headline["sha256"],
        "body_sha256": [item["sha256"] for item in body_assets],
        "phase": "authorized",
    }
    if journal_path.is_file():
        prior = json.loads(journal_path.read_text(encoding="utf-8"))
        immutable_keys = (
            "run_id",
            "pair",
            "target",
            "edit_id",
            "source_sha256",
            "headline_sha256",
            "body_sha256",
        )
        immutable_match = all(
            prior.get(key) == journal.get(key)
            for key in immutable_keys
        )
        if (
            immutable_match
            and prior.get("adapted_sha256")
            == journal.get("adapted_sha256")
        ):
            discovered_public_id = str(journal.get("public_id") or "")
            prior_evidence = prior.get("browser_evidence", {})
            if (
                discovered_public_id
                and prior.get("public_id") in (None, "")
                and prior.get("phase") == "published"
                and isinstance(prior_evidence, dict)
                and str(prior_evidence.get("public_id", ""))
                == discovered_public_id
                and str(prior_evidence.get("public_url", ""))
                == (
                    f"https://x.com/{expected_identity}/status/"
                    f"{discovered_public_id}"
                )
            ):
                # The first publish discovers X's public Article ID only
                # after the browser effect. If canonical readback then finds
                # one bounded same-ID repair gap, preserve that effect proof
                # while binding the later protected public ID into the
                # journal; otherwise the journal can never enter repair-live.
                prior["public_id"] = discovered_public_id
                _atomic_json(journal_path, prior)
            journal = prior
        elif (
            immutable_match
            and decision.get("action") == "publish"
            and prior.get("phase") == "authorized"
            and prior.get("public_id") in (None, "")
            and "browser_evidence" not in prior
        ):
            # The fixed editor is still remotely unpublished (proved by the
            # fresh preflight above), and the prior attempt never crossed the
            # browser-effect boundary. A runtime adapter repair may therefore
            # refresh only the derived adapted artifact while preserving every
            # immutable source/media/target identity.
            _atomic_json(journal_path, journal)
        else:
            raise XRepairRefused(
                "X repair journal conflicts with immutable inputs"
            )
    else:
        _atomic_json(journal_path, journal)
    if journal.get("phase") == "published":
        try:
            return _guard("reconcile", pair)
        except XRepairRefused:
            if (
                decision.get("action") != "repair-live"
                or not isinstance(protected, dict)
                or str(journal.get("public_id", "")) != public_id
            ):
                raise
    if isinstance(protected, dict):
        journal["phase"] = "unpublish-intent"
        _atomic_json(journal_path, journal)
        unpublish_evidence = browser.ensure_editable(
            target, title, protected
        )
        if (
            str(unpublish_evidence.get("edit_id", "")) != edit_id
            or str(unpublish_evidence.get("public_id", "")) != public_id
        ):
            raise XRepairRefused(
                "X unpublish evidence changed the protected identity"
            )
        journal["phase"] = "unpublished"
        journal["unpublish_evidence"] = unpublish_evidence
        _atomic_json(journal_path, journal)
    evidence = browser.replace_and_publish(
        target,
        title,
        str(adapted),
        str(cover),
        protected if isinstance(protected, dict) else None,
        readability_receipt=work / "media-readability.json",
    )
    inserted_hashes = evidence.get("inserted_image_sha256", [])
    required_body_hashes = [
        str(item["sha256"]) for item in body_assets
    ]
    if (
        str(evidence.get("edit_id", "")) != edit_id
        or evidence.get("publish_confirmed") is not True
        or not isinstance(inserted_hashes, list)
        or int(evidence.get("body_image_count", -1))
        != len(inserted_hashes)
        or not set(required_body_hashes).issubset(
            {str(value) for value in inserted_hashes}
        )
        or (
            isinstance(protected, dict)
            and str(evidence.get("public_id", "")) != public_id
        )
    ):
        raise XRepairRefused(
            "X browser evidence does not prove same-ID media publication"
        )
    journal["phase"] = "published"
    journal["browser_evidence"] = evidence
    journal["published_at_epoch"] = int(time.time())
    _atomic_json(journal_path, journal)
    return _guard("reconcile", pair)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pair",
        required=True,
        choices=("x-article/ja", "x-article/en"),
    )
    args = parser.parse_args()
    # Fail-closed PII gate at the publish boundary: scan the frozen artifacts this target is
    # about to make public. An unset ARTICLE_RUN_DIR is itself a refusal.
    gate_run_dir("x-article-inplace", os.environ.get("ARTICLE_RUN_DIR", ""), pair=args.pair)
    try:
        result = repair(args.pair)
    except XRepairRefused as error:
        message = str(error)
        if "immutable headline image is missing or changed" not in message and (
            "immutable body image" not in message
            or "is missing or changed" not in message
        ):
            raise
        try:
            quarantine = _guard(
                "quarantine-missing-media",
                args.pair,
                reason=f"x-article immutable media unavailable: {message}",
            )
        except XRepairRefused as quarantine_error:
            raise XRepairRefused(
                f"{message}; quarantine not applied: {quarantine_error}"
            ) from quarantine_error
        result = {
            "action": "quarantined-missing-media",
            "pair": args.pair,
            "reason": message,
            "state": quarantine,
        }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        XRepairRefused,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        raise SystemExit(2)
