#!/usr/bin/env python3
"""Stage and publish one immutable Dev.to article without replacing its ID."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
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

from typing import Any


class DevtoRefused(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _frontmatter(source: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", source, re.S)
    if not match:
        raise DevtoRefused("Dev.to article requires YAML frontmatter")
    values: dict[str, str] = {}
    lines = match.group(1).splitlines()
    for index, line in enumerate(lines):
        field = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", line)
        if not field:
            continue
        key, raw_value = field.groups()
        value = raw_value.strip().strip("\"'")
        if key.lower() == "tags" and not value:
            block_items: list[str] = []
            for nested in lines[index + 1 :]:
                if re.match(r"^[A-Za-z][A-Za-z0-9_-]*:\s*", nested):
                    break
                item = re.match(r"^\s+-\s+(.+?)\s*$", nested)
                if item:
                    block_items.append(item.group(1).strip().strip("\"'"))
            value = ",".join(block_items)
        values[key.lower()] = value
    return values, source[match.end() :]


def _tags(value: str) -> list[str]:
    return [
        normalized
        for item in value.strip().strip("[]").split(",")
        if (
            normalized := re.sub(
                r"[^A-Za-z0-9]",
                "",
                item.strip().strip("\"'"),
            )
        )
    ][:4]


def _metadata_and_body(draft: Path, canonical: str) -> tuple[dict[str, str], str]:
    try:
        return _frontmatter(canonical)
    except DevtoRefused:
        wrapper = draft.with_name("article-en-devto.md")
        if not wrapper.is_file() or wrapper.is_symlink():
            raise
        values, wrapped_body = _frontmatter(
            wrapper.read_text(encoding="utf-8")
        )
        if wrapped_body == canonical:
            return values, canonical
        if wrapped_body.startswith("\r\n") and wrapped_body[2:] == canonical:
            return values, canonical
        if wrapped_body.startswith("\n") and wrapped_body[1:] == canonical:
            return values, canonical
        raise DevtoRefused(
            "Dev.to metadata wrapper body does not exactly match "
            "the immutable English artifact"
        )


def _replace_mermaid(body: str, urls: list[str]) -> str:
    index = 0

    def replacement(_match: re.Match[str]) -> str:
        nonlocal index
        if index >= len(urls):
            raise DevtoRefused("every Mermaid diagram requires an immutable body asset")
        url = urls[index]
        index += 1
        return f"![Explanatory diagram {index}]({url})"

    adapted, count = re.subn(
        r"^[ \t]*```[ \t]*mermaid[^\n]*\n.*?^[ \t]*```[ \t]*$",
        replacement,
        body,
        flags=re.M | re.S | re.I,
    )
    if count == 0:
        raise DevtoRefused("Dev.to article requires a Mermaid-backed body asset")
    if count < len(urls):
        extras = "\n\n".join(
            f"![Explanatory diagram {position}]({url})"
            for position, url in enumerate(urls[count:], count + 1)
        )
        adapted = f"{adapted.rstrip()}\n\n{extras}\n"
    return adapted


def prepare(state: dict[str, Any], media_base: str) -> dict[str, Any]:
    run_id = str(state.get("run_id", ""))
    draft = Path(str(state.get("drafts", {}).get("en", {}).get("path", "")))
    if (
        not run_id
        or not draft.is_file()
        or sha256(draft) != state.get("drafts", {}).get("en", {}).get("sha256")
    ):
        raise DevtoRefused("immutable English artifact is missing or changed")
    media = state.get("media", {})
    headline = media.get("headline_image", {})
    body_assets = [
        item for item in media.get("body_assets", []) if isinstance(item, dict)
    ]
    if not body_assets:
        raise DevtoRefused("Dev.to requires immutable body media")
    for item in [headline, *body_assets]:
        path = Path(str(item.get("path", "")))
        if not path.is_file() or sha256(path) != item.get("sha256"):
            raise DevtoRefused("immutable media is missing or changed")
    values, body = _metadata_and_body(
        draft, draft.read_text(encoding="utf-8")
    )
    title = values.get("title", "").strip()
    tags = _tags(values.get("tags", ""))
    if not title or not tags:
        raise DevtoRefused("Dev.to requires title and tags")
    base = media_base.rstrip("/") + f"/{run_id}"
    body_urls = [
        f"{base}/{Path(str(item['path'])).name}" for item in body_assets
    ]
    adapted = _replace_mermaid(body, body_urls)
    marker = (
        f"<!-- article-run:{run_id} "
        f"artifact-sha256:{state['drafts']['en']['sha256']} -->"
    )
    return {
        "title": title,
        "tags": tags,
        "body_markdown": f"{adapted.rstrip()}\n\n{marker}\n",
        "main_image": f"{base}/{Path(str(headline['path'])).name}",
        "marker": f"article-run:{run_id}",
        "asset_urls": [
            f"{base}/{Path(str(headline['path'])).name}",
            *body_urls,
        ],
    }


def prepare_existing(
    state: dict[str, Any],
    media_base: str,
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Adapt frozen bytes for an in-place update of one protected live ID."""
    run_id = str(state.get("run_id", ""))
    draft = Path(
        str(state.get("drafts", {}).get("en", {}).get("path", ""))
    )
    if (
        not run_id
        or not draft.is_file()
        or sha256(draft)
        != state.get("drafts", {}).get("en", {}).get("sha256")
    ):
        raise DevtoRefused("immutable English artifact is missing or changed")
    title = str(existing.get("title") or "").strip()
    raw_tags = existing.get("tag_list")
    if isinstance(raw_tags, str):
        tags = _tags(raw_tags)
    elif isinstance(raw_tags, list):
        tags = [str(item).strip() for item in raw_tags if str(item).strip()][
            :4
        ]
    else:
        tags = []
    if not title or not tags:
        raise DevtoRefused("existing Dev.to title or tags are missing")
    media = state.get("media", {})
    headline = media.get("headline_image", {})
    body_assets = [
        item
        for item in media.get("body_assets", [])
        if isinstance(item, dict)
    ]
    if not body_assets:
        raise DevtoRefused("Dev.to repair requires immutable body media")
    for item in [headline, *body_assets]:
        path = Path(str(item.get("path", "")))
        if not path.is_file() or sha256(path) != item.get("sha256"):
            raise DevtoRefused("immutable media is missing or changed")
    base = media_base.rstrip("/") + f"/{run_id}"
    body_urls = [
        f"{base}/{Path(str(item['path'])).name}" for item in body_assets
    ]
    canonical = draft.read_text(encoding="utf-8")
    try:
        _values, body = _frontmatter(canonical)
    except DevtoRefused:
        body = canonical
    body = body.lstrip("\r\n").rstrip()
    media_markdown = "\n\n".join(
        f"![Explanatory diagram {index}]({url})"
        for index, url in enumerate(body_urls, 1)
    )
    canonical_media = re.compile(
        r"<!-- canonical-media:start -->.*?"
        r"<!-- canonical-media:end -->",
        flags=re.S,
    )
    if canonical_media.search(body):
        body = canonical_media.sub(media_markdown, body).rstrip()
    else:
        body = f"{body}\n\n{media_markdown}".rstrip()
    marker = (
        f"<!-- article-run:{run_id} "
        f"artifact-sha256:{state['drafts']['en']['sha256']} -->"
    )
    return {
        "title": title,
        "tags": tags,
        "body_markdown": f"{body}\n\n{marker}\n",
        "main_image": (
            f"{base}/{Path(str(headline['path'])).name}"
        ),
        "asset_urls": [
            f"{base}/{Path(str(headline['path'])).name}",
            *body_urls,
        ],
    }


def select_existing(rows: Any, marker: str) -> str:
    if not isinstance(rows, list):
        raise DevtoRefused("Dev.to article list is malformed")
    matches = [
        str(row.get("id"))
        for row in rows
        if isinstance(row, dict)
        and row.get("id")
        and marker in str(row.get("body_markdown", ""))
    ]
    unique = list(dict.fromkeys(matches))
    if len(unique) > 1:
        raise DevtoRefused("multiple Dev.to articles match this run")
    return unique[0] if unique else ""


def publish_payload() -> dict[str, dict[str, bool]]:
    return {"article": {"published": True}}


DEVTO_BROWSER_HEADERS = {
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


def _request(
    url: str,
    api_key: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    encoded = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=encoded,
        method=method,
        headers={
            **DEVTO_BROWSER_HEADERS,
            "api-key": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code != 422:
            raise
        detail = error.read(1000).decode("utf-8", errors="replace").strip()
        raise DevtoRefused(
            f"Dev.to API HTTP 422: {detail or error.reason}"
        ) from error


def _assets_available(urls: list[str]) -> bool:
    for url in urls:
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "writer-agent/1",
                    "Range": "bytes=0-1023",
                },
            )
            with urllib.request.urlopen(request, timeout=25) as response:
                content_type = str(response.headers.get("Content-Type", ""))
                response.read(1024)
            if response.status not in {200, 206} or not content_type.startswith("image/"):
                return False
        except Exception:
            return False
    return True


def _receipt_asset_urls(
    state_path: Path,
    state: dict[str, Any],
) -> list[str]:
    receipt_path = state_path.parent / "media-urls.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevtoRefused(
            "canonical media stager returned no valid receipt"
        ) from error
    descriptors = [
        state.get("media", {}).get("headline_image", {}),
        *[
            item
            for item in state.get("media", {}).get("body_assets", [])
            if isinstance(item, dict)
        ],
    ]
    assets = receipt.get("assets", []) if isinstance(receipt, dict) else []
    if (
        receipt.get("version") != 1
        or receipt.get("run_id") != state.get("run_id")
        or not isinstance(assets, list)
        or len(assets) != len(descriptors)
    ):
        raise DevtoRefused("canonical media receipt does not match this run")
    urls: list[str] = []
    run_id = str(state.get("run_id", ""))
    override = os.environ.get("ARTICLE_MEDIA_RAW_BASE", "").rstrip("/")
    for descriptor, asset in zip(descriptors, assets):
        if (
            not isinstance(asset, dict)
            or asset.get("path") != descriptor.get("path")
            or asset.get("sha256") != descriptor.get("sha256")
        ):
            raise DevtoRefused(
                "canonical media receipt does not match immutable assets"
            )
        url = str(asset.get("url", ""))
        suffix = f"/{run_id}/{Path(str(descriptor.get('path', ''))).name}"
        if not url.endswith(suffix):
            raise DevtoRefused("canonical media receipt URL is malformed")
        base = url[: -len(suffix)]
        if override:
            if base != override:
                raise DevtoRefused(
                    "canonical media receipt ignored configured media base"
                )
        else:
            raise DevtoRefused(
                "ARTICLE_MEDIA_RAW_BASE is required for canonical media"
            )
        urls.append(url)
    return urls


def _media_base_from_urls(
    state: dict[str, Any],
    urls: list[str],
) -> str:
    descriptors = [
        state.get("media", {}).get("headline_image", {}),
        *[
            item
            for item in state.get("media", {}).get("body_assets", [])
            if isinstance(item, dict)
        ],
    ]
    if len(urls) != len(descriptors):
        raise DevtoRefused("canonical media URL set is incomplete")
    bases = set()
    run_id = str(state.get("run_id", ""))
    for descriptor, url in zip(descriptors, urls):
        suffix = f"/{run_id}/{Path(str(descriptor.get('path', ''))).name}"
        if not url.endswith(suffix):
            raise DevtoRefused("canonical media URL does not match its asset")
        bases.add(url[: -len(suffix)])
    if len(bases) != 1:
        raise DevtoRefused("canonical media assets do not share one base")
    return bases.pop()


def _ensure_public_media(
    state_path: Path,
    state: dict[str, Any],
    urls: list[str],
) -> list[str]:
    try:
        base = _media_base_from_urls(state, urls)
    except DevtoRefused:
        base = ""
    override = os.environ.get("ARTICLE_MEDIA_RAW_BASE", "").rstrip("/")
    trusted_base = bool(override) and base == override
    if trusted_base and _assets_available(urls):
        return urls
    stager = (
        Path(__file__).parents[1]
        / "zenn-publish/current_run_zenn.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            str(stager),
            "media",
            "--state",
            str(state_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DevtoRefused(
            result.stderr.strip()
            or "canonical public media staging failed"
        )
    receipt_urls = _receipt_asset_urls(state_path, state)
    if not _assets_available(receipt_urls):
        raise DevtoRefused("canonical public media is not readable")
    return receipt_urls


def _load_state() -> tuple[dict[str, Any], Path]:
    state_path = Path(os.environ.get("ARTICLE_PUBLICATION_STATE", ""))
    if not state_path.is_file():
        raise DevtoRefused("ARTICLE_PUBLICATION_STATE is required")
    value = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DevtoRefused("publication state is malformed")
    return value, state_path


def _guard(command: str, target: str) -> dict[str, Any]:
    guard = Path(__file__).parents[1] / "publication-guard.py"
    args = [
        sys.executable,
        str(guard),
        command,
        "--pair",
        "devto/en",
    ]
    if command == "preflight":
        args.extend(
            ["--target-kind", "devto-article-id", "--target", target]
        )
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise DevtoRefused(result.stderr.strip() or "Dev.to publication guard refused")
    return json.loads(result.stdout)


def _identity(api_key: str) -> None:
    me = _request("https://dev.to/api/users/me", api_key)
    expected = os.environ.get("DEVTO_ACCOUNT_HANDLE", "").strip().lstrip("@").lower()
    if not expected:
        raise DevtoRefused("DEVTO_ACCOUNT_HANDLE is required")
    if not isinstance(me, dict) or str(me.get("username", "")).lower() != expected:
        raise DevtoRefused("Dev.to API identity does not match configured account")


def stage() -> dict[str, Any]:
    state, state_path = _load_state()
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        raise DevtoRefused("DEVTO_API_KEY is required")
    _identity(api_key)
    media_base = os.environ.get("ARTICLE_MEDIA_RAW_BASE", "")
    if not media_base:
        raise DevtoRefused("ARTICLE_MEDIA_RAW_BASE is required")
    prepared = prepare(state, media_base)
    resolved_urls = _ensure_public_media(
        state_path,
        state,
        prepared["asset_urls"],
    )
    if resolved_urls != prepared["asset_urls"]:
        prepared = prepare(
            state,
            _media_base_from_urls(state, resolved_urls),
        )
        if prepared["asset_urls"] != resolved_urls:
            raise DevtoRefused(
                "canonical media receipt changed during preparation"
            )
    registered = state.get("pairs", {}).get("devto/en", {})
    target = str(registered.get("target", ""))
    if not target:
        rows = _request(
            "https://dev.to/api/articles/me/all?per_page=1000", api_key
        )
        target = select_existing(rows, str(prepared["marker"]))
    article_payload = {
        "article": {
            "title": prepared["title"],
            "tags": prepared["tags"],
            "published": False,
            "body_markdown": prepared["body_markdown"],
            "main_image": prepared["main_image"],
        }
    }
    if target:
        try:
            live = _request(f"https://dev.to/api/articles/{target}", api_key)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            live = {}
        if not isinstance(live, dict) or not (
            live.get("published_at") or live.get("published_timestamp")
        ):
            value = _request(
                f"https://dev.to/api/articles/{target}",
                api_key,
                method="PUT",
                payload=article_payload,
            )
        else:
            value = live
    else:
        value = _request(
            "https://dev.to/api/articles",
            api_key,
            method="POST",
            payload=article_payload,
        )
    target = str(value.get("id", "") if isinstance(value, dict) else "")
    if not target.isascii() or not target.isdigit() or int(target) <= 0:
        raise DevtoRefused("Dev.to staging returned no stable article ID")
    if os.environ.get("ARTICLE_RUN_DIR"):
        register = Path(__file__).parents[1] / "publication-guard.py"
        result = subprocess.run(
            [
                sys.executable,
                str(register),
                "register-intent",
                "--pair",
                "devto/en",
                "--target-kind",
                "devto-article-id",
                "--target",
                target,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise DevtoRefused(
                result.stderr.strip() or "Dev.to target registration failed"
            )
    return {
        "status": "draft",
        "article_id": target,
        "dashboard_url": f"https://dev.to/dashboard/{target}",
    }


def publish_existing(target: str) -> dict[str, Any]:
    state, _ = _load_state()
    entry = state.get("pairs", {}).get("devto/en", {})
    if str(entry.get("target", "")) != target:
        raise DevtoRefused("Dev.to target is not the persisted exact8 intent")
    if entry.get("status") == "repair-required":
        return repair_existing(target)
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        raise DevtoRefused("DEVTO_API_KEY is required")
    _identity(api_key)
    if entry.get("status") == "ambiguous":
        recovery = _guard("recover-ambiguous", target)
        if recovery.get("status") == "repair-required":
            return repair_existing(target)
        if recovery.get("status") == "live":
            receipt = recovery.get("receipt", {})
            return {
                "action": "skip-live",
                "live_url": str(
                    receipt.get("live_url")
                    if isinstance(receipt, dict)
                    else ""
                ),
            }
        if recovery.get("status") == "unavailable":
            return {
                "action": "quarantined-unresolved",
                "pair": "devto/en",
                "reason": str(recovery.get("error", "")),
            }
    decision = _guard("preflight", target)
    if decision.get("action") == "skip-live":
        return decision
    _request(
        f"https://dev.to/api/articles/{target}",
        api_key,
        method="PUT",
        payload=publish_payload(),
    )
    _wait_for_public_article(target, api_key)
    return _guard("reconcile", target)


def _wait_for_public_article(
    target: str,
    api_key: str,
    *,
    max_attempts: int = 5,
) -> dict[str, Any]:
    """Wait out Dev.to's draft-list → public-API propagation gap.

    Immediately after a successful publish PUT, the article can disappear
    from /me/unpublished before /api/articles/{id} becomes readable. Calling
    the publication guard inside that gap freezes a real publish as
    ambiguous. Poll the immutable numeric ID first; the guard still performs
    the full anonymous content/media/identity receipt once visibility exists.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            article = _request(
                f"https://dev.to/api/articles/{target}",
                api_key,
            )
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            article = {}
        if (
            isinstance(article, dict)
            and str(article.get("id", "")) == target
            and (
                article.get("published_at")
                or article.get("published_timestamp")
            )
        ):
            return article
        if attempt < max_attempts:
            time.sleep(2)
    raise DevtoRefused(
        "Dev.to publish is not visible on the public article API after "
        f"{max_attempts} attempts"
    )


def repair_existing(target: str) -> dict[str, Any]:
    state, state_path = _load_state()
    entry = state.get("pairs", {}).get("devto/en", {})
    protected = entry.get("existing_publication", {})
    if (
        str(entry.get("target", "")) != target
        or str(protected.get("public_id", "")) != target
    ):
        raise DevtoRefused(
            "Dev.to repair target is not the protected live identity"
        )
    api_key = os.environ.get("DEVTO_API_KEY", "")
    if not api_key:
        raise DevtoRefused("DEVTO_API_KEY is required")
    _identity(api_key)
    decision = _guard("preflight", target)
    if decision.get("action") == "skip-live":
        return decision
    if decision.get("action") != "repair-live":
        raise DevtoRefused("Dev.to in-place repair was not authorized")
    existing = _request(
        f"https://dev.to/api/articles/{target}",
        api_key,
    )
    if (
        not isinstance(existing, dict)
        or str(existing.get("id", "")) != target
        or not (
            existing.get("published_at")
            or existing.get("published_timestamp")
        )
    ):
        raise DevtoRefused("protected Dev.to article is not live")
    media_base = os.environ.get("ARTICLE_MEDIA_RAW_BASE", "")
    if not media_base:
        raise DevtoRefused("ARTICLE_MEDIA_RAW_BASE is required")
    prepared = prepare_existing(state, media_base, existing)
    resolved_urls = _ensure_public_media(
        state_path,
        state,
        prepared["asset_urls"],
    )
    if resolved_urls != prepared["asset_urls"]:
        prepared = prepare_existing(
            state,
            _media_base_from_urls(state, resolved_urls),
            existing,
        )
        if prepared["asset_urls"] != resolved_urls:
            raise DevtoRefused(
                "canonical media receipt changed during repair"
            )
    value = _request(
        f"https://dev.to/api/articles/{target}",
        api_key,
        method="PUT",
        payload={
            "article": {
                "title": prepared["title"],
                "tags": prepared["tags"],
                "published": True,
                "body_markdown": prepared["body_markdown"],
                "main_image": prepared["main_image"],
            }
        },
    )
    if not isinstance(value, dict) or str(value.get("id", "")) != target:
        raise DevtoRefused("Dev.to repair changed or lost the protected ID")
    return _guard("reconcile", target)


def main() -> int:
    if (
        len(sys.argv) not in {2, 3}
        or sys.argv[1] not in {"stage", "go", "repair"}
    ):
        print(
            "usage: devto.py stage | go <article-id> | repair <article-id>",
            file=sys.stderr,
        )
        return 2
    # Fail-closed PII gate at the publish boundary: scan the frozen artifacts this target is
    # about to make public. An unset ARTICLE_RUN_DIR is itself a refusal.
    gate_run_dir("devto", os.environ.get("ARTICLE_RUN_DIR", ""), pair="devto/en")
    try:
        if sys.argv[1] == "stage":
            result = stage()
        elif sys.argv[1] == "go":
            result = publish_existing(sys.argv[2])
        else:
            result = repair_existing(sys.argv[2])
    except (DevtoRefused, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
