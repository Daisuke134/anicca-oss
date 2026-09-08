#!/usr/bin/env python3
"""Read platform reality for one stable publication target.

No function here publishes.  A result is verified only when the platform itself identifies the
target as live or still a draft.  Network/browser uncertainty remains ``unknown`` and therefore
causes the mandatory guard to refuse a replay.
"""

from __future__ import annotations

import json
import html
import hashlib
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SUBSTACK_HTTP_DIR = SCRIPT_DIR / "substack-publish"
if str(SUBSTACK_HTTP_DIR) not in sys.path:
    sys.path.insert(0, str(SUBSTACK_HTTP_DIR))
from substack_http import json_request as substack_json_request
from substack_http import text_request as substack_text_request
from substack_http import bytes_request as substack_bytes_request
from media_integrity import (
    center_crop_content_proof,
    NOTE_EYECATCH_RATIO_WINDOW,
    content_proof,
    descriptor_from_file,
)


DESTINATION_PROOF_FLAGS = {
    "note/ja": ("eyecatch_verified", "body_media_verified"),
    "zenn-article/ja": ("body_media_verified",),
    "devto/en": ("body_media_verified",),
    "substack/ja": ("body_media_verified",),
    "substack/en": ("body_media_verified",),
    "x-article/ja": ("cover_verified", "body_media_verified"),
    "x-article/en": ("cover_verified", "body_media_verified"),
    "x-post/ja": ("timeline_verified", "emoji_verified"),
}


def _state_asset_hashes(state: dict[str, Any], pair: str) -> list[str]:
    if pair == "x-post/ja":
        return []
    media = state.get("media", {})
    hashes = [
        str(item.get("sha256", ""))
        for item in media.get("body_assets", [])
        if isinstance(item, dict)
    ]
    if pair != "zenn-article/ja":
        hashes.insert(0, str(media.get("headline_image", {}).get("sha256", "")))
    return hashes


def finalize_live(
    state: dict[str, Any],
    pair: str,
    target: str,
    observed: dict[str, Any],
) -> dict[str, Any]:
    """Mint a receipt only from destination-owned content and media readback."""
    if observed.get("status") != "live" or observed.get("verified") is not True:
        return observed
    if observed.get("content_verified") is not True:
        return {"status": "unknown", "reason": "canonical-content-readback-failed"}
    if pair == "note/ja" and not (
        observed.get("monetization_verified") is True
        and observed.get("price") == 500
    ):
        return {"status": "unknown", "reason": "note-monetization-readback-failed"}
    if pair.startswith("substack/") and not (
        observed.get("monetization_verified") is True
        and observed.get("audience") == "only_paid"
        and observed.get("paywall_verified") is True
    ):
        return {"status": "unknown", "reason": "substack-monetization-readback-failed"}
    if pair != "x-post/ja" and observed.get("asset_verified") is not True:
        return {"status": "unknown", "reason": "public-asset-readback-failed"}
    if any(observed.get(flag) is not True for flag in DESTINATION_PROOF_FLAGS[pair]):
        return {"status": "unknown", "reason": "destination-media-readback-failed"}
    if (
        observed.get("identity_verified") is not True
        or not isinstance(observed.get("destination_identity"), str)
        or not isinstance(observed.get("identity_source"), str)
    ):
        return {"status": "unknown", "reason": "destination-identity-readback-failed"}
    expected_assets = _state_asset_hashes(state, pair)
    proofs = observed.get("asset_proofs")
    if (
        not isinstance(proofs, list)
        or len(proofs) != len(expected_assets)
        or [
            proof.get("expected_sha256") if isinstance(proof, dict) else None
            for proof in proofs
        ]
        != expected_assets
    ):
        return {"status": "unknown", "reason": "public-asset-content-proof-failed"}
    entry = state.get("pairs", {}).get(pair, {})
    lang = str(entry.get("lang", ""))
    artifact = (
        state.get("x_post", {})
        if pair == "x-post/ja"
        else state.get("drafts", {}).get(lang, {})
    )
    result = {
        **observed,
        "stable_target": target,
        "artifact_sha256": str(artifact.get("sha256", "")),
        "language": lang,
        "asset_hashes": _state_asset_hashes(state, pair),
        "asset_proofs": proofs,
        "asset_urls": [proof["remote_url"] for proof in proofs],
    }
    if pair == "x-post/ja":
        result["asset_verified"] = True
    return result


def note_media_evidence_gap(
    observed: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Expose only a paid, content-matching same-key note media repair."""
    live_url = str(observed.get("live_url", ""))
    public_id = str(observed.get("public_id", ""))
    identity = str(observed.get("destination_identity", ""))
    parsed = urlparse(live_url)
    if not (
        result.get("status") == "unknown"
        and result.get("reason")
        in {
            "public-asset-readback-failed",
            "destination-media-readback-failed",
        }
        and observed.get("status") == "live"
        and observed.get("verified") is True
        and observed.get("content_verified") is True
        and observed.get("authenticated_content_verified") is True
        and observed.get("monetization_verified") is True
        and observed.get("eyecatch_verified") is True
        and observed.get("body_media_verified") is False
        and observed.get("identity_verified") is True
        and observed.get("identity_source") == "note-public-canonical-url"
        and observed.get("source") == "note-api+anonymous-public-html"
        and parsed.scheme == "https"
        and parsed.hostname == "note.com"
        and parsed.path == f"/{identity}/n/{public_id}"
        and public_id.startswith("n")
    ):
        return None
    return {
        "status": "live-media-mismatch",
        "reason": str(result["reason"]),
        "verified": True,
        "live_url": live_url,
        "public_id": public_id,
        "destination_identity": identity,
        "identity_verified": True,
        "identity_source": "note-public-canonical-url",
        "source": "note-api+anonymous-public-html",
    }


def devto_media_evidence_gap(
    observed: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Expose only a fully identified, content-matching Dev.to media gap."""
    live_url = str(observed.get("live_url", ""))
    parsed_live = urlparse(live_url)
    if not (
        result.get("status") == "unknown"
        and result.get("reason")
        in {
            "public-asset-readback-failed",
            "destination-media-readback-failed",
        }
        and observed.get("status") == "live"
        and observed.get("verified") is True
        and observed.get("content_verified") is True
        and observed.get("identity_verified") is True
        and observed.get("identity_source")
        == "devto-authenticated-article-api"
        and observed.get("source")
        == "devto-authenticated-api+anonymous-public-html"
        and parsed_live.scheme == "https"
        and parsed_live.hostname == "dev.to"
        and parsed_live.path.startswith(
            f"/{observed.get('destination_identity')}/"
        )
        and str(observed.get("public_id", "")).isdigit()
    ):
        return None
    return {
        "status": "live-media-mismatch",
        "reason": result["reason"],
        "verified": True,
        "live_url": live_url,
        "public_id": str(observed["public_id"]),
        "destination_identity": observed["destination_identity"],
        "identity_verified": True,
        "identity_source": observed["identity_source"],
        "source": observed["source"],
    }


def x_table_evidence_gap(
    state: dict[str, Any],
    pair: str,
    target: str,
    observed: dict[str, Any],
    *,
    table_ok: bool,
    expected_table_count: int,
    text_verified: bool,
    title_rendered: bool,
) -> dict[str, Any] | None:
    """Reclassify an already-public X Article as ``live-media-mismatch``
    only when every proof ``finalize_live`` requires already holds EXCEPT
    the table-image journal a later in-place repair records.

    This never fabricates a proof: it re-runs ``finalize_live`` with
    ``content_verified`` forced past only the table gate, so every other
    invariant (identity, cover, body, exact asset-hash equality) is still
    independently re-enforced by ``finalize_live`` itself. A genuine content
    mismatch, an unrendered title, or any other missing proof returns
    ``None`` and the caller keeps refusing.
    """
    if not (
        expected_table_count > 0
        and not table_ok
        and text_verified
        and title_rendered
    ):
        return None
    fully_proven = finalize_live(
        state, pair, target, {**observed, "content_verified": text_verified}
    )
    if fully_proven.get("status") != "live":
        return None
    return {
        "status": "live-media-mismatch",
        "reason": "x-table-evidence-missing",
        "verified": True,
        "live_url": observed["live_url"],
        "public_id": observed["public_id"],
        "destination_identity": observed["destination_identity"],
        "identity_verified": True,
        "identity_source": observed["identity_source"],
        "source": observed["source"],
    }


def x_media_evidence_gap(
    state: dict[str, Any],
    pair: str,
    target: str,
    observed: dict[str, Any],
    *,
    text_verified: bool,
    title_rendered: bool,
    table_ok: bool,
) -> dict[str, Any] | None:
    """Expose a strictly identified public X Article for same-ID media repair.

    The article's immutable title/text, table evidence, canonical account
    identity, and authenticated public URL must already be proven.  Only a
    destination-media failure may cross this boundary; content or identity
    uncertainty remains unknown and cannot trigger a mutation.
    """
    if not (
        pair in {"x-article/ja", "x-article/en"}
        and text_verified
        and title_rendered
        and table_ok
        and observed.get("status") == "live"
        and observed.get("verified") is True
        and observed.get("identity_verified") is True
        and observed.get("identity_source")
        == "x-public-canonical-account-path"
        and observed.get("source") == "x-authenticated-cdp-public-article"
    ):
        return None
    result = finalize_live(
        state,
        pair,
        target,
        {**observed, "content_verified": True},
    )
    if result.get("reason") not in {
        "public-asset-readback-failed",
        "destination-media-readback-failed",
    }:
        return None
    return {
        "status": "live-media-mismatch",
        "reason": result["reason"],
        "verified": True,
        "live_url": observed["live_url"],
        "public_id": observed["public_id"],
        "destination_identity": observed["destination_identity"],
        "identity_verified": True,
        "identity_source": observed["identity_source"],
        "source": observed["source"],
    }


def x_content_evidence_gap(
    state: dict[str, Any],
    pair: str,
    target: str,
    observed: dict[str, Any],
    *,
    remote_text: str,
    artifact: Path,
    title_rendered: bool,
) -> dict[str, Any] | None:
    """Expose only an exact final measurable-CTA omission for same-ID repair.

    A generic content mismatch remains unknown. Repair authority is granted
    only when every preceding canonical block is present in order, the sole
    measurable CTA is the final source block, and every non-content receipt
    invariant still passes independently.
    """
    try:
        source = Path(artifact).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    product_landing_url = os.environ.get("ARTICLE_PRODUCT_LANDING_URL", "").strip()
    landing = urlparse(product_landing_url)
    if landing.scheme not in {"http", "https"} or not landing.netloc:
        return None
    cta_lines = [
        line
        for line in source.splitlines()
        if (
            product_landing_url.rstrip("/") in line
            and "product_id=" in line
            and "run_id=" in line
            and "artifact_id=" in line
            and "variant_id=" in line
            and "click_id=" in line
        )
    ]
    blocks = _source_blocks(source)
    if (
        pair not in {"x-article/ja", "x-article/en"}
        or not title_rendered
        or observed.get("status") != "live"
        or observed.get("verified") is not True
        or observed.get("identity_verified") is not True
        or observed.get("identity_source")
        != "x-public-canonical-account-path"
        or observed.get("source") != "x-authenticated-cdp-public-article"
        or len(cta_lines) != 1
        or len(blocks) < 2
        or _visible_text(cta_lines[0]) != blocks[-1]
        or not _blocks_match(remote_text, blocks[:-1])
        or _blocks_match(remote_text, blocks)
        or re.sub(r"\s+", "", blocks[-1])
        in re.sub(r"\s+", "", _visible_text(remote_text))
    ):
        return None
    fully_proven = finalize_live(
        state,
        pair,
        target,
        {**observed, "content_verified": True},
    )
    if fully_proven.get("status") != "live":
        return None
    return {
        "status": "live-content-mismatch",
        "reason": "x-final-cta-missing",
        "verified": True,
        "live_url": observed["live_url"],
        "public_id": observed["public_id"],
        "destination_identity": observed["destination_identity"],
        "identity_verified": True,
        "identity_source": observed["identity_source"],
        "source": observed["source"],
    }


def _visible_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
    value = re.sub(
        r"<img\b[^>]*\balt=[\"']([^\"']*)[\"'][^>]*>",
        r" \1 ",
        value,
        flags=re.I,
    )
    value = re.sub(r"<img\b[^>]*>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"!\[([^]]*)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    while True:
        cleaned = re.sub(
            r"^[ \t]*(?:#{1,6}|>|[-+*]|\d+[.)])[ \t]+",
            "",
            value,
        )
        if cleaned == value:
            break
        value = cleaned
    value = re.sub(r"[`*_~]", "", value)
    return " ".join(value.split())


def _source_blocks(source: str) -> list[str]:
    if source.startswith("---"):
        source = re.sub(r"^---\r?\n.*?\r?\n---(?:\r?\n|$)", "", source, count=1, flags=re.S)
    source = re.sub(
        r"^[ \t]*```[ \t]*mermaid[^\n]*\n.*?^[ \t]*```[ \t]*$",
        "",
        source,
        flags=re.M | re.S | re.I,
    )
    blocks: list[str] = []
    for raw in source.splitlines():
        stripped = raw.strip()
        if (
            not stripped
            or stripped.startswith("<!--")
            or stripped in {">", "-", "+", "*"}
            or re.fullmatch(r"(?:\|?\s*:?-{3,}:?\s*)+\|?", stripped)
            or stripped.startswith("```")
        ):
            continue
        values = (
            [cell.strip() for cell in stripped.strip("|").split("|")]
            if "|" in stripped
            else [stripped]
        )
        blocks.extend(
            normalized
            for value in values
            if (normalized := _visible_text(value))
        )
    return blocks


def _blocks_match(remote: str, blocks: list[str]) -> bool:
    rendered = re.sub(r"\s+", "", _visible_text(remote))
    if not blocks:
        return False
    position = 0
    for block in blocks:
        canonical = re.sub(r"\s+", "", block)
        found = rendered.find(canonical, position)
        if found < 0:
            return False
        position = found + len(canonical)
    return True


def content_matches(remote: str, artifact: Path) -> bool:
    """Require every canonical visible source block in order in public readback."""
    blocks = _source_blocks(Path(artifact).read_text(encoding="utf-8"))
    return _blocks_match(remote, blocks)


def note_content_matches_text(remote: str, source: str) -> bool:
    """note renders markdown tables as PNG figures, so table cells never
    appear as page text (proved live 2026-07-25). Match everything else in
    order, exactly like the X text path."""
    without_table_rows = "\n".join(
        line
        for line in source.splitlines()
        if not line.strip().startswith("|")
    )
    return _blocks_match(remote, _source_blocks(without_table_rows))


def note_content_matches(remote: str, artifact: Path) -> bool:
    return note_content_matches_text(
        remote, Path(artifact).read_text(encoding="utf-8")
    )


def x_content_matches(remote: str, artifact: Path) -> bool:
    """Match X text while table rows are proven through derived image readback."""
    source = Path(artifact).read_text(encoding="utf-8")
    without_table_rows = "\n".join(
        line
        for line in source.splitlines()
        if not line.strip().startswith("|")
    )
    return _blocks_match(remote, _source_blocks(without_table_rows))


X_MISSING_STATUS_MARKERS = (
    "this page doesn’t exist",
    "this page doesn't exist",
    "このページは存在しません",
)


def x_status_page_is_missing(body: str, account: str) -> bool:
    """A draft's editor URL redirects to a status URL that does not exist.

    Measured live 2026-07-26: X sends /compose/articles/edit/<id> to
    /<account>/status/<id>, and for an unpublished article that status page
    renders X's not-found empty state. Reading that as ambiguous froze the
    pair and starved x-article/en, which waits on the JA publish time. The
    verdict only counts while the page proves we are authenticated as the
    owning account.
    """
    if not body or not account:
        return False
    text = body.casefold()
    if f"@{account.casefold()}" not in text:
        return False
    return any(marker.casefold() in text for marker in X_MISSING_STATUS_MARKERS)


class _ImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        source = next(
            (value for key, value in attrs if key.lower() == "src"),
            None,
        )
        if isinstance(source, str) and source.startswith("https://"):
            self.urls.append(html.unescape(source))


def image_urls(remote_html: str) -> list[str]:
    parser = _ImageParser()
    parser.feed(remote_html)
    return list(dict.fromkeys(parser.urls))


def markdown_image_urls(remote_markdown: str) -> list[str]:
    return list(
        dict.fromkeys(
            html.unescape(match)
            for match in re.findall(
                r"!\[[^\]]*\]\((https://[^)\s]+)",
                remote_markdown,
            )
        )
    )


def hosted_asset_count(remote_html: str, *, hosts: set[str] | None = None) -> int:
    urls = image_urls(remote_html)
    if hosts is None:
        return len(urls)
    return sum(1 for url in urls if (urlparse(url).hostname or "").lower() in hosts)


def get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    if (urlparse(url).hostname or "").lower().endswith(".substack.com") or (
        urlparse(url).hostname or ""
    ).lower() == "substack.com":
        value = substack_json_request(
            "GET", url, headers or {"User-Agent": "Mozilla/5.0"}, timeout=25
        )
        if not isinstance(value, dict):
            raise ValueError("remote did not return a JSON object")
        return value
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("remote did not return a JSON object")
    return value


def get_text(url: str, headers: dict[str, str] | None = None) -> str:
    if (urlparse(url).hostname or "").lower().endswith(".substack.com") or (
        urlparse(url).hostname or ""
    ).lower() == "substack.com":
        value, _ = substack_text_request(
            url,
            headers or {"User-Agent": "Mozilla/5.0"},
            timeout=30,
            final_url=True,
        )
        return value
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def get_text_with_final_url(
    url: str, headers: dict[str, str] | None = None
) -> tuple[str, str]:
    """Read a page and retain the URL after urllib follows redirects."""
    if (urlparse(url).hostname or "").lower().endswith(".substack.com") or (
        urlparse(url).hostname or ""
    ).lower() == "substack.com":
        value, final_url = substack_text_request(
            url,
            headers or {"User-Agent": "Mozilla/5.0"},
            timeout=30,
            final_url=True,
        )
        return value, final_url or url
    request = urllib.request.Request(
        url, headers=headers or {"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return (
            response.read().decode("utf-8", errors="replace"),
            str(response.geturl()),
        )


def _artifact_path(state: dict[str, Any], pair: str) -> Path:
    if pair == "x-post/ja":
        return Path(str(state.get("x_post", {}).get("path", "")))
    lang = str(state.get("pairs", {}).get(pair, {}).get("lang", ""))
    return Path(str(state.get("drafts", {}).get(lang, {}).get("path", "")))


def _body_asset_count(state: dict[str, Any]) -> int:
    return len(
        [
            item
            for item in state.get("media", {}).get("body_assets", [])
            if isinstance(item, dict)
        ]
    )


def _expected_identity(state: dict[str, Any], pair: str) -> str:
    return str(state.get("destination_identities", {}).get(pair, ""))


def _substack_publication_host(value: Any) -> str:
    """Extract an explicit publication host from an authenticated API object."""
    if isinstance(value, dict):
        for key in (
            "subdomain",
            "host",
            "domain",
            "url",
            "publication",
            "draft_publication",
        ):
            host = _substack_publication_host(value.get(key))
            if host:
                return host
        return ""
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if "://" in candidate:
        candidate = urlparse(candidate).hostname or ""
    candidate = candidate.rstrip("/")
    if candidate and "." not in candidate:
        candidate = f"{candidate}.substack.com"
    return candidate if candidate.endswith(".substack.com") else ""


def _substack_draft_publication(data: dict[str, Any]) -> str:
    for key in (
        "publication",
        "draft_publication",
        "publication_host",
        "publication_subdomain",
        "subdomain",
    ):
        host = _substack_publication_host(data.get(key))
        if host:
            return host
    return ""


def _substack_authenticated_publication(
    data: dict[str, Any], publication: str, headers: dict[str, str]
) -> str:
    """Resolve a draft's numeric publication_id through the authenticated host."""
    direct = _substack_draft_publication(data)
    if direct:
        return direct
    publication_id = data.get("publication_id")
    if not str(publication_id).isdigit():
        return ""
    try:
        profile = get_json(
            f"https://{publication}/api/v1/publication",
            headers,
        )
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError):
        return ""
    if not isinstance(profile, dict) or str(profile.get("id")) != str(publication_id):
        return ""
    return _substack_publication_host(profile)


def _expected_asset_descriptors(
    state: dict[str, Any], pair: str
) -> list[dict[str, Any]]:
    if pair == "x-post/ja":
        return []
    media = state.get("media", {})
    descriptors = [
        dict(item)
        for item in media.get("body_assets", [])
        if isinstance(item, dict)
    ]
    if pair != "zenn-article/ja":
        descriptors.insert(0, dict(media.get("headline_image", {})))
    return descriptors


def get_bytes(url: str) -> bytes:
    host = (urlparse(url).hostname or "").lower()
    if host in {"substack-post-media.s3.amazonaws.com", "substackcdn.com"}:
        data, content_type = substack_bytes_request(
            url, {"User-Agent": "Mozilla/5.0"}, timeout=30
        )
        if not content_type.startswith("image/"):
            raise ValueError("public asset is not an image")
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("public image exceeds verification limit")
        return data
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = str(response.headers.get("Content-Type", "")).lower()
        data = response.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("public image exceeds verification limit")
    if not content_type.startswith("image/"):
        raise ValueError("public asset is not an image")
    return data


def prove_remote_descriptors(
    expected: list[dict[str, Any]],
    urls: list[str],
    cover_crop_window: tuple[float, float] | None = None,
    fetcher=None,
) -> list[dict[str, Any]]:
    unique_urls = list(dict.fromkeys(url for url in urls if isinstance(url, str)))
    fetch = fetcher or get_bytes
    remote = [(url, fetch(url)) for url in unique_urls]
    used: set[int] = set()
    proofs: list[dict[str, Any]] = []
    for descriptor_index, descriptor in enumerate(expected):
        match: tuple[int, dict[str, Any]] | None = None
        for index, (url, data) in enumerate(remote):
            if index in used:
                continue
            proof = content_proof(descriptor, data, url)
            if (
                proof is None
                and cover_crop_window is not None
                and descriptor_index == 0
            ):
                # A platform cover is a center crop of the source, not the
                # whole image; plain dHash cannot prove it.
                proof = center_crop_content_proof(
                    descriptor, data, url, ratio_window=cover_crop_window
                )
            if proof is not None:
                if match is None:
                    match = (index, proof)
                if proof["match_method"] == "exact-sha256":
                    match = (index, proof)
                    break
        if match is None:
            return []
        used.add(match[0])
        proofs.append(match[1])
    return proofs


def prove_remote_assets(
    state: dict[str, Any], pair: str, urls: list[str]
) -> list[dict[str, Any]]:
    return prove_remote_descriptors(
        _expected_asset_descriptors(state, pair),
        urls,
        cover_crop_window=(
            NOTE_EYECATCH_RATIO_WINDOW if pair == "note/ja" else None
        ),
    )


def note_asset_evidence(
    state: dict[str, Any],
    eyecatch: str,
    body_images: list[str],
) -> dict[str, Any]:
    """Keep cover and body proofs independent so a body gap stays repairable."""
    expected = _expected_asset_descriptors(state, "note/ja")
    if not expected:
        return {
            "asset_proofs": [],
            "asset_verified": False,
            "body_media_verified": False,
            "eyecatch_verified": False,
        }
    eyecatch_proofs = prove_remote_descriptors(
        expected[:1],
        [eyecatch],
        cover_crop_window=NOTE_EYECATCH_RATIO_WINDOW,
    )
    body_proofs = prove_remote_descriptors(expected[1:], body_images)
    eyecatch_ok = len(eyecatch_proofs) == 1
    body_ok = len(body_proofs) == len(expected) - 1
    return {
        "asset_proofs": [*eyecatch_proofs, *body_proofs],
        "asset_verified": eyecatch_ok and body_ok,
        "body_media_verified": body_ok,
        "eyecatch_verified": eyecatch_ok,
    }


def _x_markdown_table_count(artifact: Path) -> int:
    return sum(
        1
        for line in Path(artifact).read_text(encoding="utf-8").splitlines()
        if re.fullmatch(
            r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*",
            line,
        )
    )


def _x_table_descriptors(
    state: dict[str, Any],
    pair: str,
) -> list[dict[str, Any]]:
    entry = state.get("pairs", {}).get(pair, {})
    lang = str(entry.get("lang", ""))
    artifact = _artifact_path(state, pair)
    expected_count = _x_markdown_table_count(artifact)
    if expected_count == 0:
        return []
    work = (
        Path(str(state.get("run_dir", "")))
        / "gates"
        / "x-inplace-repair"
        / lang
    )
    try:
        journal = json.loads(
            (work / "journal.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return []
    paths = sorted((work / "prepared" / "assets").glob("tbl*.png"))
    if (
        journal.get("phase") != "published"
        or len(paths) != expected_count
        or any(not path.is_file() for path in paths)
    ):
        return []
    descriptors = [descriptor_from_file(path) for path in paths]
    inserted = {
        str(value)
        for value in journal.get("browser_evidence", {}).get(
            "inserted_image_sha256", []
        )
    }
    if any(
        str(descriptor.get("sha256", "")) not in inserted
        for descriptor in descriptors
    ):
        return []
    return descriptors


def _candidate_url(value: Any) -> str:
    if isinstance(value, str) and value.startswith("https://"):
        return value
    if isinstance(value, dict):
        for key in ("url", "src", "original", "large"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("https://"):
                return candidate
    return ""


def devto_image_origin(value: Any) -> str:
    """Recover the exact public origin embedded in dev.to's cover proxy."""
    candidate = _candidate_url(value)
    if not candidate:
        return ""
    decoded = unquote(candidate)
    marker = decoded.rfind("/https://")
    return decoded[marker + 1 :] if marker >= 0 else candidate


def _urls_alive(urls: list[str], *, minimum: int) -> bool:
    unique = list(dict.fromkeys(urls))
    if len(unique) < minimum:
        return False
    for url in unique:
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-2047"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                content_type = str(response.headers.get("Content-Type", "")).lower()
                first = response.read(2048)
            if response.status not in {200, 206} or (
                not content_type.startswith("image/")
                and "<svg" not in first.decode("utf-8", errors="ignore").lower()
            ):
                return False
        except Exception:
            return False
    return True


def _payload_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    return "\n".join(
        value
        for key in keys
        if isinstance((value := payload.get(key)), str) and value.strip()
    )


def note_authenticated_data(target: str) -> dict[str, Any]:
    """Read the owner's full paid body; anonymous note API returns only teaser."""
    cookie_path = Path.home() / ".cloak/note-work/note-cookies.json"
    cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
    if not isinstance(cookies, dict) or not cookies:
        raise ValueError("note owner cookie cache is empty")
    cookie_header = "; ".join(
        f"{key}={value}" for key, value in cookies.items()
    )
    response = get_json(
        f"https://note.com/api/v3/notes/{target}",
        headers={"Cookie": cookie_header},
    )
    data = response.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("authenticated note response is malformed")
    return data


def note(target: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    expected_identity = _expected_identity(state or {}, "note/ja")
    try:
        data = get_json(f"https://note.com/api/v3/notes/{target}").get("data", {})
    except urllib.error.HTTPError as error:
        # Draft keys are not exposed by the public notes API.  A 404 is safe to
        # classify as draft-only only when the authenticated staging ledger has
        # independently recorded the same n-prefixed key.
        if error.code == 404 and target.startswith("n"):
            ledger_path = Path.home() / ".cloak" / "note-work" / "draft-ledger.json"
            try:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ledger = {}
            if any(
                isinstance(value, dict)
                and value.get("key") == target
                and value.get("account") == expected_identity
                for value in ledger.values()
            ):
                return {
                    "status": "not-live",
                    "verified": True,
                    "destination_identity": expected_identity,
                    "identity_verified": True,
                    "identity_source": "authenticated-note-draft-ledger",
                    "source": "note-api-404-plus-draft-ledger",
                }
        raise
    if not isinstance(data, dict):
        return {"status": "unknown", "reason": "malformed-note-response"}
    status = data.get("status")
    if status == "published":
        live_url = data.get("note_url") or data.get("url")
        if isinstance(live_url, str) and live_url.startswith("http"):
            if state is None:
                return {
                    "status": "unknown",
                    "reason": "managed-state-required-for-live-readback",
                }
            public_html = get_text(live_url)
            public_body = _payload_text(
                data, ("body", "body_html", "content", "content_html")
            )
            try:
                authenticated = note_authenticated_data(target)
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                urllib.error.URLError,
            ):
                return {
                    "status": "unknown",
                    "reason": "authenticated-note-content-readback-failed",
                }
            body = _payload_text(
                authenticated,
                ("body", "body_html", "content", "content_html"),
            )
            canonical_readback = "\n".join(
                value
                for value in (
                    str(data.get("name") or data.get("title") or ""),
                    body,
                )
                if value
            )
            artifact = _artifact_path(state, "note/ja")
            eyecatch = _candidate_url(data.get("eyecatch"))
            if not eyecatch:
                match = re.search(
                    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    public_html,
                    flags=re.I,
                )
                eyecatch = html.unescape(match.group(1)) if match else ""
            note_images = [
                url
                for url in image_urls(
                    body + "\n" + public_body + "\n" + public_html
                )
                if (urlparse(url).hostname or "").lower() == "assets.st-note.com"
            ]
            body_images = [url for url in note_images if url != eyecatch]
            media_evidence = note_asset_evidence(
                state, eyecatch, body_images
            )
            proofs = media_evidence["asset_proofs"]
            identity_ok = (
                bool(expected_identity)
                and urlparse(live_url).path.startswith(f"/{expected_identity}/n/")
            )
            observed = {
                "status": "live",
                "live_url": live_url,
                "verified": True,
                "public_id": str(data.get("key") or target),
                "published_at": str(
                    data.get("publish_at")
                    or data.get("published_at")
                    or ""
                ),
                "content_verified": note_content_matches(
                    canonical_readback, artifact
                ),
                "authenticated_content_verified": note_content_matches(
                    canonical_readback, artifact
                ),
                "monetization_verified": (
                    data.get("price") == 500
                    and data.get("is_limited") is False
                    and data.get("is_trial") is False
                ),
                "price": data.get("price"),
                "asset_verified": media_evidence["asset_verified"],
                "eyecatch_verified": media_evidence["eyecatch_verified"],
                "body_media_verified": media_evidence["body_media_verified"],
                "asset_urls": [proof["remote_url"] for proof in proofs],
                "asset_proofs": proofs,
                "destination_identity": expected_identity,
                "identity_verified": identity_ok,
                "identity_source": "note-public-canonical-url",
                "source": "note-api+anonymous-public-html",
            }
            result = finalize_live(
                state,
                "note/ja",
                target,
                observed,
            )
            return note_media_evidence_gap(observed, result) or result
        return {"status": "unknown", "reason": "note-live-without-url"}
    if status in {"draft", "unpublished"}:
        author = data.get("user") if isinstance(data.get("user"), dict) else {}
        identity = str(author.get("urlname") or author.get("username") or "")
        return {
            "status": "not-live",
            "verified": True,
            "destination_identity": identity,
            "identity_verified": identity == expected_identity,
            "identity_source": "note-authenticated-draft-api",
            "source": "note-api",
        }
    return {"status": "unknown", "reason": f"note-status:{status}"}


def zenn(target: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    expected_identity = _expected_identity(state or {}, "zenn-article/ja")
    if not expected_identity:
        return {"status": "unknown", "reason": "missing-protected-zenn-identity"}
    data = get_json(
        f"https://zenn.dev/api/articles?username={expected_identity}&order=latest"
    )
    articles = data.get("articles", [])
    if not isinstance(articles, list):
        return {"status": "unknown", "reason": "malformed-zenn-response"}
    match = next((item for item in articles if isinstance(item, dict) and item.get("slug") == target), None)
    if match:
        if state is None:
            return {
                "status": "unknown",
                "reason": "managed-state-required-for-live-readback",
            }
        live_url = f"https://zenn.dev/{expected_identity}/articles/{target}"
        public_html = get_text(live_url)
        try:
            detail = get_json(f"https://zenn.dev/api/articles/{target}")
        except Exception:
            detail = {}
        remote = (
            json.dumps(match, ensure_ascii=False)
            + "\n"
            + json.dumps(detail, ensure_ascii=False)
            + "\n"
            + public_html
        )
        urls = image_urls(remote)
        proofs = prove_remote_assets(state, "zenn-article/ja", urls)
        diagram_ok = len(proofs) == _body_asset_count(state)
        return finalize_live(
            state,
            "zenn-article/ja",
            target,
            {
                "status": "live",
                "live_url": live_url,
                "verified": True,
                "public_id": str(match.get("id") or target),
                "published_at": str(
                    match.get("published_at")
                    or detail.get("published_at")
                    or ""
                ),
                "content_verified": content_matches(
                    remote, _artifact_path(state, "zenn-article/ja")
                ),
                "asset_verified": diagram_ok,
                "body_media_verified": diagram_ok,
                "asset_urls": [proof["remote_url"] for proof in proofs],
                "asset_proofs": proofs,
                "destination_identity": expected_identity,
                "identity_verified": True,
                "identity_source": "zenn-username-scoped-api",
                "source": "zenn-api+anonymous-public-html",
            },
        )
    return {
        "status": "not-live",
        "verified": True,
        "destination_identity": expected_identity,
        "identity_verified": True,
        "identity_source": "zenn-username-scoped-api",
        "source": "zenn-api",
    }


def substack_body_readback(value: str) -> tuple[str, list[str]]:
    """Extract visible text and media from Substack's ProseMirror JSON."""
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value, image_urls(value)
    texts: list[str] = []
    urls: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        text = node.get("text")
        if isinstance(text, str):
            texts.append(text)
        attrs = node.get("attrs")
        if isinstance(attrs, dict):
            source = attrs.get("src")
            if isinstance(source, str) and source.startswith("https://"):
                urls.append(source)
            raw_html = attrs.get("html")
            if isinstance(raw_html, str):
                texts.append(_visible_text(raw_html))
                urls.extend(image_urls(raw_html))
        visit(node.get("content"))

    visit(parsed)
    return " ".join(texts), list(dict.fromkeys(urls))


def substack(
    target: str, pair: str, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    publication = _expected_identity(state or {}, pair)
    if not publication:
        return {"status": "unknown", "reason": "missing-protected-substack-identity"}
    cookie = os.environ.get(
        "SUBSTACK_SESSION_COOKIE_EN" if pair == "substack/en" else "SUBSTACK_SESSION_COOKIE_JA",
        "",
    ).strip() or os.environ.get("SUBSTACK_SESSION_COOKIE", "")
    if not cookie:
        return {"status": "unknown", "reason": "missing-substack-cookie"}
    data = get_json(
        f"https://{publication}/api/v1/drafts/{target}",
        {"User-Agent": "Mozilla/5.0", "Cookie": cookie},
    )
    request_headers = {"User-Agent": "Mozilla/5.0", "Cookie": cookie}
    draft_publication = _substack_authenticated_publication(
        data, publication, request_headers
    )
    if not draft_publication:
        return {
            "status": "unknown",
            "reason": "substack-draft-publication-identity-readback-missing",
        }
    if draft_publication != publication:
        return {
            "status": "unknown",
            "reason": "substack-draft-publication-identity-mismatch",
            "destination_identity": draft_publication,
            "identity_verified": False,
            "identity_source": "substack-authenticated-draft-readback",
        }
    is_published = data.get("is_published")
    post_date = data.get("post_date")
    slug = data.get("slug") or data.get("draft_slug")
    if (is_published is True or post_date) and isinstance(slug, str) and slug:
        if state is None:
            return {
                "status": "unknown",
                "reason": "managed-state-required-for-live-readback",
            }
        live_url = f"https://{publication}/p/{slug}"
        public_html, final_url = get_text_with_final_url(live_url)
        final = urlparse(final_url)
        if (
            final.scheme != "https"
            or final.hostname != publication
            or not final.path.startswith("/p/")
        ):
            return {
                "status": "unknown",
                "reason": "substack-live-canonical-identity-mismatch",
                "live_url": final_url,
                "destination_identity": final.hostname or "",
                "identity_verified": False,
                "identity_source": "substack-public-canonical-redirect",
            }
        body = next(
            (
                value
                for key in ("draft_body", "body_html", "body", "content")
                if isinstance((value := data.get(key)), str)
                and value.strip()
            ),
            "",
        )
        try:
            from substack_paid_payload import verify_paid_draft_response

            verify_paid_draft_response(data)
            monetization_verified = True
        except Exception:
            monetization_verified = False
        readable_body, structured_urls = substack_body_readback(body)
        remote = readable_body + "\n" + public_html
        body_urls = [
            url
            for url in list(
                dict.fromkeys(
                    [*structured_urls, *image_urls(public_html)]
                )
            )
            if "substack" in (urlparse(url).hostname or "").lower()
            or "amazonaws.com" in (urlparse(url).hostname or "").lower()
        ]
        proofs = prove_remote_assets(state, pair, body_urls)
        body_ok = len(proofs) == _body_asset_count(state) + 1
        result = finalize_live(
            state,
            pair,
            target,
            {
                "status": "live",
                "live_url": live_url,
                "verified": True,
                "public_id": str(data.get("id") or target),
                "published_at": str(
                    post_date or data.get("published_at") or ""
                ),
                "content_verified": content_matches(
                    remote, _artifact_path(state, pair)
                ),
                "monetization_verified": monetization_verified,
                "audience": data.get("audience"),
                "paywall_verified": monetization_verified,
                "asset_verified": body_ok,
                "body_media_verified": body_ok,
                "asset_urls": [proof["remote_url"] for proof in proofs],
                "asset_proofs": proofs,
                "destination_identity": publication,
                "identity_verified": final.hostname == publication,
                "identity_source": "substack-draft-and-public-canonical-readback",
                "source": "substack-draft-api+anonymous-public-html",
            },
        )
        # Our article is authenticated-live but its public media is not the
        # immutable canonical set: expose a repairable shape instead of a
        # bare unknown so bounded recovery can route the same-ID media
        # repair. The receipt boundary itself is unchanged — finalize_live
        # still refuses to mint a receipt for this state.
        if result.get("status") == "unknown" and result.get("reason") in (
            "public-asset-readback-failed",
            "destination-media-readback-failed",
        ):
            return {
                "status": "live-media-mismatch",
                "reason": result["reason"],
                "verified": True,
                "live_url": live_url,
                "public_id": str(data.get("id") or target),
                "destination_identity": publication,
                "identity_verified": final.hostname == publication,
                "identity_source": "substack-draft-and-public-canonical-readback",
                "source": "substack-draft-api+anonymous-public-html",
            }
        return result
    # Missing keys are a malformed/unknown response, not proof of a draft.  Only the API's
    # explicit boolean false authorizes the first publish side effect.
    if is_published is False:
        return {
            "status": "not-live",
            "verified": True,
            "destination_identity": publication,
            "identity_verified": True,
            "identity_source": "protected-substack-authenticated-draft-api",
            "source": "substack-draft-api",
        }
    return {"status": "unknown", "reason": "ambiguous-substack-draft"}


def devto(target: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    expected_identity = _expected_identity(state or {}, "devto/en")
    if not expected_identity:
        return {"status": "unknown", "reason": "missing-protected-devto-identity"}
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        return {"status": "unknown", "reason": "missing-devto-api-key"}
    headers = {
        "api-key": api_key,
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        ),
        "Origin": "https://dev.to",
        "Referer": "https://dev.to/settings/extensions",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    def _devto_unpublished_verdict() -> dict[str, Any] | None:
        """dev.to returns 200 with published:false for an owned draft, so the
        404 path below never fired for a staged-but-unpublished article and the
        probe fell through to public asset readback (2026-07-26)."""
        request = urllib.request.Request(
            "https://dev.to/api/articles/me/unpublished?per_page=1000",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            rows = json.load(response)
        if not isinstance(rows, list):
            return None
        match = next(
            (
                row
                for row in rows
                if isinstance(row, dict) and str(row.get("id", "")) == target
            ),
            None,
        )
        if match is None:
            return None
        user = match.get("user") if isinstance(match.get("user"), dict) else {}
        return {
            "status": "not-live",
            "verified": True,
            "destination_identity": str(user.get("username") or ""),
            "identity_verified": True,
            "identity_source": "devto-authenticated-unpublished-api",
            "source": "devto-authenticated-unpublished-api",
        }

    try:
        data = get_json(f"https://dev.to/api/articles/{target}", headers)
        if isinstance(data, dict) and data.get("published") is False:
            verdict = _devto_unpublished_verdict()
            if verdict is not None:
                return verdict
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        request = urllib.request.Request(
            "https://dev.to/api/articles/me/unpublished?per_page=1000",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=25) as response:
            rows = json.load(response)
        match = next(
            (
                row
                for row in rows
                if isinstance(row, dict) and str(row.get("id", "")) == target
            ),
            None,
        ) if isinstance(rows, list) else None
        if match is not None:
            user = match.get("user") if isinstance(match.get("user"), dict) else {}
            identity = str(user.get("username") or "")
            return {
                "status": "not-live",
                "verified": True,
                "destination_identity": identity,
                "identity_verified": identity == expected_identity,
                "identity_source": "devto-authenticated-unpublished-api",
                "source": "devto-authenticated-unpublished-api",
            }
        return {
            "status": "unknown",
            "reason": "devto-target-missing-from-owned-drafts",
        }
    published_at = data.get("published_at") or data.get("published_timestamp")
    if not published_at:
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        identity = str(user.get("username") or "")
        return {
            "status": "not-live",
            "verified": True,
            "destination_identity": identity,
            "identity_verified": identity == expected_identity,
            "identity_source": "devto-authenticated-article-api",
            "source": "devto-authenticated-article-api",
        }
    if state is None:
        return {
            "status": "unknown",
            "reason": "managed-state-required-for-live-readback",
        }
    live_url = str(data.get("url") or "")
    public_html = get_text(live_url)
    remote = _payload_text(data, ("body_markdown", "body_html")) + "\n" + public_html
    body_markdown = _payload_text(data, ("body_markdown",))
    body_html = _payload_text(data, ("body_html",))
    body_urls = list(
        dict.fromkeys(
            [
                *markdown_image_urls(body_markdown),
                *image_urls(body_html),
            ]
        )
    )
    displayed_headline_url = _candidate_url(
        data.get("main_image") or data.get("cover_image")
    )
    headline_url = devto_image_origin(displayed_headline_url)
    proofs = prove_remote_assets(
        state, "devto/en", [headline_url, *body_urls]
    )
    headline_ok = (
        bool(proofs)
        and proofs[0]["remote_url"] == headline_url
        and (
            displayed_headline_url == headline_url
            or quote(headline_url, safe="") in displayed_headline_url
        )
    )
    body_ok = (
        len(proofs) == _body_asset_count(state) + 1
        and all(proof["remote_url"] in body_urls for proof in proofs[1:])
    )
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    identity = str(user.get("username") or "")
    observed_live = {
        "status": "live",
        "live_url": live_url,
        "verified": True,
        "public_id": str(data.get("id") or target),
        "published_at": str(published_at),
        "content_verified": content_matches(
            remote, _artifact_path(state, "devto/en")
        ),
        "asset_verified": headline_ok and body_ok,
        "body_media_verified": body_ok,
        "asset_urls": [proof["remote_url"] for proof in proofs],
        "asset_proofs": proofs,
        "destination_identity": identity,
        "identity_verified": identity == expected_identity,
        "identity_source": "devto-authenticated-article-api",
        "source": "devto-authenticated-api+anonymous-public-html",
    }
    result = finalize_live(
        state,
        "devto/en",
        target,
        observed_live,
    )
    return devto_media_evidence_gap(observed_live, result) or result


def _x_account(state: dict[str, Any], pair: str) -> str:
    return _expected_identity(state, pair).strip().lstrip("@")


def _x_article_id(target: str) -> str:
    match = re.search(r"/(?:edit/)?([0-9A-Za-z_-]+)/*$", target)
    return match.group(1) if match else ""


def x_public_candidate(
    state: dict[str, Any],
    pair: str,
    target: str,
) -> tuple[str, str]:
    account = _x_account(state, pair)
    entry = state.get("pairs", {}).get(pair, {})
    protected = (
        entry.get("existing_publication")
        if isinstance(entry, dict) and entry.get("target") == target
        else None
    )
    if isinstance(protected, dict):
        public_id = str(protected.get("public_id", ""))
        live_url = str(protected.get("live_url", ""))
        parsed = urlparse(live_url)
        if (
            public_id
            and parsed.scheme == "https"
            and parsed.hostname == "x.com"
            and parsed.path.rstrip("/")
            == f"/{account}/article/{public_id}"
        ):
            return live_url, public_id
        return "", ""
    article_id = _x_article_id(target)
    lang = str(entry.get("lang", "")) if isinstance(entry, dict) else ""
    journal_path = (
        Path(str(state.get("run_dir", "")))
        / "gates"
        / "x-inplace-repair"
        / lang
        / "journal.json"
    )
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        journal = {}
    evidence = (
        journal.get("browser_evidence", {})
        if isinstance(journal, dict)
        else {}
    )
    journal_public_id = str(evidence.get("public_id", ""))
    journal_public_url = str(evidence.get("public_url", ""))
    if (
        article_id
        and journal.get("phase") == "published"
        and journal.get("run_id") == state.get("run_id")
        and journal.get("target") == target
        and str(journal.get("edit_id", "")) == article_id
        and journal_public_id
        and journal_public_url
        == f"https://x.com/{account}/status/{journal_public_id}"
    ):
        return (
            f"https://x.com/{account}/article/{journal_public_id}",
            journal_public_id,
        )
    if not account or not article_id:
        return "", ""
    # X resolves a published article from its DRAFT id at /i/article/<id>,
    # redirecting to the canonical /<account>/article/<public-id> — the two
    # ids differ. Measured live 2026-07-26: three JA articles were already
    # public while the probe checked /<account>/article/<draft-id> and
    # /status/<draft-id>, so no receipt was ever minted. An empty expected id
    # means "read the public id off the canonical URL".
    return f"https://x.com/i/article/{article_id}", ""


def x_editable_draft_evidence(
    current: str,
    target: str,
    *,
    title_count: int,
    composer_count: int,
    publish_count: int,
    profile_href: str,
    account: str,
) -> bool:
    return (
        re.fullmatch(
            r"https://x\.com/compose/articles/edit/[0-9]{8,}",
            target.rstrip("/"),
        )
        is not None
        and current.rstrip("/") == target.rstrip("/")
        and title_count == 1
        and composer_count == 1
        and publish_count >= 1
        and profile_href.strip("/") == account
    )


def x_post_effect_uncertain(state: dict[str, Any]) -> bool:
    path = (
        Path(str(state.get("run_dir", "")))
        / "gates"
        / "x-post-effect.json"
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(value, dict)
        and value.get("version") == 1
        and value.get("run_id") == state.get("run_id")
        and value.get("phase") in {"effect-possible", "target-known"}
    )


def x_article(
    target: str, pair: str, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    # Imported lazily: non-X publishers use the system Python, while X calls this through the
    # existing venv-cloak interpreter that already owns Playwright and the authenticated CDP tab.
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
    page = browser.contexts[0].new_page()
    browser_image_bodies: dict[str, bytes] = {}

    def capture_browser_image(response) -> None:
        if "pbs.twimg.com" not in response.url:
            return
        try:
            if str(response.headers.get("content-type", "")).lower().startswith("image/"):
                browser_image_bodies[response.url] = response.body()
        except Exception:
            # The image may be served from an already-loaded browser cache
            # entry.  The DOM still proves it rendered; a later fetch path
            # will decide whether bytes are available for hash verification.
            return

    page.on("response", capture_browser_image)

    def browser_get_bytes(url: str) -> bytes:
        cached = browser_image_bodies.get(url)
        if cached is not None:
            return cached
        response = page.request.get(url, timeout=30000)
        if not response.ok:
            raise ValueError(f"public image request failed: {response.status}")
        content_type = str(response.headers.get("content-type", "")).lower()
        data = response.body()
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("public image exceeds verification limit")
        if not content_type.startswith("image/"):
            raise ValueError("public asset is not an image")
        return data

    try:
        page.goto(target, wait_until="domcontentloaded", timeout=50000)
        page.wait_for_timeout(5000)
        current = page.url
        body = page.locator("body").inner_text(timeout=10000)
        canonical = page.locator('link[rel="canonical"]').get_attribute("href") if page.locator('link[rel="canonical"]').count() else None
        live_url = canonical or current
        parsed = urlparse(live_url)
        is_edit = "/edit" in parsed.path or "/compose" in parsed.path
        article_id = _x_article_id(target)
        expected_public_id = article_id
        account = _x_account(state or {}, pair)
        title_fields = page.locator(
            'textarea[placeholder="Add a title"]'
        )
        composer = page.locator('[data-testid="composer"]')
        publish_controls = page.get_by_text(
            re.compile(r"^(Publish|公開する)$")
        )
        profile = page.locator(
            '[data-testid="AppTabBar_Profile_Link"]'
        )
        profile_href = (
            str(profile.first.get_attribute("href") or "")
            if profile.count()
            else ""
        )
        if x_editable_draft_evidence(
            current,
            target,
            title_count=title_fields.count(),
            composer_count=composer.count(),
            publish_count=publish_controls.count(),
            profile_href=profile_href,
            account=account,
        ):
            if state is None:
                return {
                    "status": "unknown",
                    "reason": "managed-state-required-for-x-draft-readback",
                }
            artifact = _artifact_path(state, pair)
            try:
                editor_title = title_fields.first.input_value()
                editor_body = composer.first.inner_text()
            except Exception:
                return {
                    "status": "unknown",
                    "reason": "x-draft-content-readback-failed",
                }
            content_verified = x_content_matches(
                f"{editor_title}\n{editor_body}", artifact
            )
            if not content_verified:
                return {
                    "status": "unknown",
                    "reason": "x-draft-content-mismatch",
                    "target": target,
                    "destination_identity": account,
                    "identity_verified": True,
                    "identity_source": "x-authenticated-edit-url",
                }
            return {
                "status": "not-live",
                "verified": True,
                "target": target,
                "content_verified": True,
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                "destination_identity": account,
                "identity_verified": True,
                "identity_source": "x-authenticated-edit-url",
                "source": "x-cdp-saved-article-editor",
            }
        if is_edit and article_id:
            if not account:
                return {"status": "unknown", "reason": "missing-protected-x-identity"}
            candidate, expected_public_id = x_public_candidate(
                state or {}, pair, target
            )
            if not candidate:
                return {
                    "status": "unknown",
                    "reason": "missing-protected-x-publication",
                }
            page.goto(candidate, wait_until="domcontentloaded", timeout=50000)
            page.wait_for_timeout(4000)
            current = page.url
            # Re-read the page after navigating: every check below reasons
            # about the page we are on now, not the editor we came from.
            body = page.locator("body").inner_text(timeout=10000)
            canonical = (
                page.locator('link[rel="canonical"]').get_attribute("href")
                if page.locator('link[rel="canonical"]').count()
                else None
            )
            live_url = canonical or current
            parsed = urlparse(live_url)
            is_edit = "/edit" in parsed.path or "/compose" in parsed.path
        if not is_edit and parsed.netloc.lower() in {
            "x.com", "www.x.com", "twitter.com", "www.twitter.com"
        } and ("/article/" in parsed.path or "/i/articles/" in parsed.path):
            if state is None:
                return {
                    "status": "unknown",
                    "reason": "managed-state-required-for-live-readback",
                }
            # X Article pages hydrate slowly; a fixed short wait read a
            # half-rendered body and failed canonical readback on articles
            # that were fully public (measured 2026-07-26: 4s failed, 9s
            # matched every block).
            try:
                page.wait_for_selector(
                    '[data-testid="twitterArticleRichTextView"],'
                    '[data-testid="articleBody"],article',
                    timeout=30_000,
                )
                page.wait_for_timeout(3_000)
            except Exception:
                pass
            body_scope = page.locator(
                '[data-testid="twitterArticleRichTextView"],'
                '[data-testid="articleBody"],article'
            )
            if body_scope.count() < 1:
                return {
                    "status": "unknown",
                    "reason": "x-article-body-scope-ambiguous",
                }
            article = body_scope.first
            remote = article.inner_text() + "\n" + article.inner_html()
            title = page.locator(
                '[data-testid="twitter-article-title"],main h1'
            )
            if title.count():
                remote = title.first.inner_text() + "\n" + remote
            body_images = article.locator("img[src]").evaluate_all(
                """els => els.filter(e => e.naturalWidth > 0 && e.naturalHeight > 0)
                            .map(e => e.src)"""
            )
            main_images = page.locator("main img[src]").evaluate_all(
                """els => els.filter(e => e.naturalWidth > 0 && e.naturalHeight > 0)
                            .map(e => e.src)"""
            )
            headline = dict(
                state.get("media", {}).get("headline_image", {})
            )
            cover_proof = next(
                (
                    proof
                    for url in map(str, main_images)
                    if (
                        proof := center_crop_content_proof(
                            headline,
                            browser_get_bytes(url),
                            url,
                        )
                    )
                    is not None
                ),
                None,
            )
            body_descriptors = [
                dict(item)
                for item in state.get("media", {}).get(
                    "body_assets", []
                )
                if isinstance(item, dict)
            ]
            body_proofs = prove_remote_descriptors(
                body_descriptors,
                list(map(str, body_images)),
                fetcher=browser_get_bytes,
            )
            proofs = (
                [cover_proof, *body_proofs]
                if cover_proof is not None
                else []
            )
            artifact = _artifact_path(state, pair)
            expected_table_count = _x_markdown_table_count(artifact)
            table_descriptors = _x_table_descriptors(state, pair)
            table_proofs = prove_remote_descriptors(
                table_descriptors,
                list(map(str, body_images)),
                fetcher=browser_get_bytes,
            )
            table_ok = (
                len(table_descriptors) == expected_table_count
                and len(table_proofs) == expected_table_count
            )
            body_ok = (
                len(body_proofs) == _body_asset_count(state)
                and all(
                    proof["remote_url"] in set(map(str, body_images))
                    for proof in body_proofs
                )
            )
            cover_ok = (
                cover_proof is not None
                and cover_proof["remote_url"] in set(map(str, main_images))
            )
            time_node = page.locator("time[datetime]").first
            published_at = (
                str(time_node.get_attribute("datetime") or "")
                if time_node.count()
                else ""
            )
            public_id = _x_article_id(live_url)
            # An empty expectation means "discover the public id from the
            # canonical URL"; a recorded one must still match exactly.
            if expected_public_id and public_id != expected_public_id:
                return {
                    "status": "unknown",
                    "reason": "x-public-id-mismatch",
                }
            text_verified = x_content_matches(remote, artifact)
            observed_live = {
                "status": "live",
                "live_url": live_url,
                "verified": True,
                "public_id": public_id,
                "published_at": published_at,
                "content_verified": text_verified and table_ok,
                "asset_verified": body_ok and cover_ok,
                "cover_verified": cover_ok,
                "body_media_verified": body_ok,
                "table_media_verified": table_ok,
                "table_asset_proofs": table_proofs,
                "asset_urls": [proof["remote_url"] for proof in proofs],
                "asset_proofs": proofs,
                "destination_identity": account,
                "identity_verified": urlparse(live_url).path.startswith(
                    f"/{account}/"
                ),
                "identity_source": "x-public-canonical-account-path",
                "source": "x-authenticated-cdp-public-article",
            }
            result = finalize_live(state, pair, target, observed_live)
            if result.get("status") != "live":
                media_gap = x_media_evidence_gap(
                    state,
                    pair,
                    target,
                    observed_live,
                    text_verified=text_verified,
                    title_rendered=title.count() >= 1,
                    table_ok=table_ok,
                )
                if media_gap is not None:
                    return media_gap
                content_gap = x_content_evidence_gap(
                    state,
                    pair,
                    target,
                    observed_live,
                    remote_text=remote,
                    artifact=artifact,
                    title_rendered=title.count() >= 1,
                )
                if content_gap is not None:
                    return content_gap
                # X Articles publish without ever calling this script when a
                # different flow inserts them (measured live 2026-07-26:
                # daily-2026-07-26 was public with matching title/text but no
                # in-place-repair journal existed yet to prove its table
                # images, freezing every future repair in a circular
                # refusal). A public-but-table-unproven article is a
                # narrower, still-strict shape: never a plain live receipt.
                gap = x_table_evidence_gap(
                    state,
                    pair,
                    target,
                    observed_live,
                    table_ok=table_ok,
                    expected_table_count=expected_table_count,
                    text_verified=text_verified,
                    title_rendered=title.count() >= 1,
                )
                if gap is not None:
                    return gap
            return result
        if x_status_page_is_missing(body, account):
            return {
                "status": "not-live",
                "verified": True,
                "target": target,
                "source": "x-authenticated-missing-status-page",
                "identity_source": "x-authenticated-account-navbar",
                "identity_verified": True,
                "destination_identity": account,
                "observed_url": current,
            }
        return {"status": "unknown", "reason": "ambiguous-x-draft-view", "observed_url": current}
    finally:
        page.close()
        playwright.stop()


def x_post(target: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    if state is None:
        return {
            "status": "unknown",
            "reason": "managed-state-required-for-x-post-readback",
        }
    from playwright.sync_api import sync_playwright

    expected = _artifact_path(state, "x-post/ja").read_text(encoding="utf-8").strip()
    account = _x_account(state, "x-post/ja")
    if not account:
        return {"status": "unknown", "reason": "missing-protected-x-identity"}
    account_url = f"https://x.com/{account}"
    playwright = sync_playwright().start()
    page = None
    try:
        browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
        page = browser.contexts[0].new_page()
        page.goto(account_url, wait_until="domcontentloaded", timeout=50000)
        page.wait_for_timeout(4000)
        rows = page.locator('[data-testid="tweet"]').evaluate_all(
            """tweets => tweets.map(tweet => {
              const node = tweet.querySelector('[data-testid="tweetText"]');
              const clone = node ? node.cloneNode(true) : null;
              if (clone) clone.querySelectorAll('img[alt]').forEach(
                image => image.replaceWith(document.createTextNode(image.alt))
              );
              return {
                url: (tweet.querySelector('a[href*="/status/"]') || {}).href || '',
                text: clone ? clone.innerText : '',
                published_at: ((tweet.querySelector('time[datetime]') || {}).dateTime || '')
              };
            })"""
        )
        normalized = " ".join(expected.split())
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and " ".join(str(row.get("text", "")).split()) == normalized
            and re.search(r"/status/[0-9]+(?:$|[?#])", str(row.get("url", "")))
        ]
        if not matches:
            if x_post_effect_uncertain(state):
                return {
                    "status": "unknown",
                    "reason": "x-post-effect-uncertain-awaiting-timeline-readback",
                }
            return {
                "status": "not-live",
                "verified": True,
                "source": "x-authenticated-account-timeline",
                # Carry identity so a frozen slot can be recovered: the
                # timeline we just read IS the authenticated account.
                "identity_source": "x-authenticated-account-timeline",
                "identity_verified": True,
                "destination_identity": account,
            }
        if len(matches) != 1:
            return {"status": "unknown", "reason": "ambiguous-x-post-readback"}
        match = matches[0]
        live_url = str(match["url"]).split("?", 1)[0]
        status_id = live_url.rstrip("/").rsplit("/", 1)[-1]
        return finalize_live(
            state,
            "x-post/ja",
            target,
            {
                "status": "live",
                "live_url": live_url,
                "verified": True,
                "public_id": status_id,
                "status_id": status_id,
                "published_at": str(match.get("published_at", "")),
                "content_verified": True,
                "asset_verified": True,
                "timeline_verified": True,
                "emoji_verified": str(match.get("text", "")) == expected,
                "asset_proofs": [],
                "destination_identity": account,
                "identity_verified": urlparse(live_url).path.startswith(
                    f"/{account}/status/"
                ),
                "identity_source": "x-authenticated-account-timeline",
                "source": "x-authenticated-account-timeline",
            },
        )
    finally:
        if page is not None:
            page.close()
        playwright.stop()


def probe(
    pair: str, target: str, state: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        if pair == "note/ja":
            return note(target, state)
        if pair in {"x-article/ja", "x-article/en"}:
            return x_article(target, pair, state)
        if pair == "x-post/ja":
            return x_post(target, state)
        if pair == "zenn-article/ja":
            return zenn(target, state)
        if pair == "devto/en":
            return devto(target, state)
        if pair in {"substack/ja", "substack/en"}:
            return substack(target, pair, state)
        return {"status": "unknown", "reason": "unsupported-pair"}
    except Exception as error:  # uncertainty must be a refusal, never guessed not-live
        return {"status": "unknown", "reason": f"remote-probe-error:{type(error).__name__}:{error}"}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: publication_remote.py <pair> <stable-target>", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(probe(sys.argv[1], sys.argv[2]), ensure_ascii=False, separators=(",", ":")))
