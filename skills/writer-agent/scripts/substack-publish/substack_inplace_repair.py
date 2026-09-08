#!/usr/bin/env python3
"""Install immutable media on one protected live Substack draft ID."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
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

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from substack_http import json_request


class SubstackRepairRefused(RuntimeError):
    pass


SCRIPTS = Path(__file__).resolve().parents[1]


def _paid_payload_builder():
    module_path = (
        SCRIPTS / "substack-publish/substack_paid_payload.py"
    )
    spec = importlib.util.spec_from_file_location(
        "article_substack_paid_payload", module_path
    )
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise SubstackRepairRefused(
            "Substack paid payload builder is unavailable"
        )
    spec.loader.exec_module(module)
    return module.build_paid_draft_payload


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state() -> dict[str, Any]:
    path = Path(os.environ.get("ARTICLE_PUBLICATION_STATE", ""))
    if not path.is_file():
        raise SubstackRepairRefused(
            "ARTICLE_PUBLICATION_STATE is required"
        )
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SubstackRepairRefused("publication state is malformed")
    return value


def _publication() -> str:
    value = os.environ.get("SUBSTACK_PUBLICATION", "").strip().lower()
    if not value.endswith(".substack.com"):
        raise SubstackRepairRefused("Substack publication host is invalid")
    return value


def _cookie() -> str:
    value = os.environ.get("SUBSTACK_SESSION_COOKIE", "")
    if not value:
        raise SubstackRepairRefused("SUBSTACK_SESSION_COOKIE is required")
    return value


def _request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> Any:
    url = (
        path
        if path.startswith("https://")
        else f"https://{_publication()}{path}"
    )
    return json_request(
        method,
        url,
        {
            "Cookie": _cookie(),
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        payload,
        timeout=45,
    )


def _identity() -> int:
    profile = _request(
        "GET", "https://substack.com/api/v1/user/profile/self"
    )
    publication = _publication().removesuffix(".substack.com")
    users = profile.get("publicationUsers", []) if isinstance(profile, dict) else []
    matches = [
        item
        for item in users
        if isinstance(item, dict)
        and str(
            item.get("publication", {}).get("subdomain", "")
        ).lower()
        == publication
        and str(item.get("user_id", "")).isdigit()
    ]
    if len(matches) != 1:
        raise SubstackRepairRefused(
            "authenticated Substack publication identity is ambiguous"
        )
    return int(matches[0]["user_id"])


def upload_image(path: str, publication: str, cookie: str) -> str:
    """Reuse the established Substack image uploader without forking it."""
    uploader = SCRIPTS / "_shared/embed-mermaid-substack.py"
    spec = importlib.util.spec_from_file_location(
        "article_substack_image_uploader", uploader
    )
    module = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise SubstackRepairRefused("Substack image uploader is unavailable")
    spec.loader.exec_module(module)
    return str(module.upload_image(path, publication, cookie))


def _guard(command: str, pair: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "publication-guard.py"),
            command,
            "--pair",
            pair,
            *(
                [
                    "--target-kind",
                    "substack-draft-id",
                    "--target",
                    str(
                        _state()
                        .get("pairs", {})
                        .get(pair, {})
                        .get("target", "")
                    ),
                ]
                if command == "preflight"
                else []
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SubstackRepairRefused(
            result.stderr.strip() or "Substack publication guard refused"
        )
    return json.loads(result.stdout)


def adapt_body(
    state: dict[str, Any],
    lang: str,
    headline_url: str,
    body_urls: list[str],
) -> str:
    descriptor = state.get("drafts", {}).get(lang, {})
    source = Path(str(descriptor.get("path", "")))
    if (
        not source.is_file()
        or sha256(source) != descriptor.get("sha256")
    ):
        raise SubstackRepairRefused(
            f"immutable {lang} artifact is missing or changed"
        )
    if not headline_url or not body_urls:
        raise SubstackRepairRefused(
            "Substack repair requires headline and body media"
        )
    media = [f"![Headline image]({headline_url})"]
    media.extend(
        f"![Explanatory diagram {index}]({url})"
        for index, url in enumerate(body_urls, 1)
    )
    marker = (
        f"<!-- article-run:{state['run_id']} "
        f"artifact-sha256:{descriptor['sha256']} -->"
    )
    return (
        source.read_text(encoding="utf-8").rstrip()
        + "\n\n"
        + "\n\n".join(media)
        + f"\n\n{marker}\n"
    )


def repair(pair: str) -> dict[str, Any]:
    if pair not in {"substack/ja", "substack/en"}:
        raise SubstackRepairRefused("invalid Substack repair pair")
    state = _state()
    entry = state.get("pairs", {}).get(pair, {})
    protected = entry.get("existing_publication", {})
    target = str(entry.get("target", ""))
    if (
        not target.isdigit()
        or str(protected.get("public_id", "")) != target
    ):
        raise SubstackRepairRefused(
            "Substack repair target is not the protected live ID"
        )
    live_url = str(protected.get("live_url", ""))
    parsed = urlparse(live_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _publication()
        or not parsed.path.startswith("/p/")
    ):
        raise SubstackRepairRefused(
            "protected Substack live URL is invalid"
        )
    owned_user_id = _identity()
    decision = _guard("preflight", pair)
    if decision.get("action") == "skip-live":
        return decision
    if decision.get("action") != "repair-live":
        raise SubstackRepairRefused(
            "Substack in-place repair was not authorized"
        )
    existing = _request("GET", f"/api/v1/drafts/{target}")
    slug = parsed.path.removeprefix("/p/").strip("/")
    if (
        not isinstance(existing, dict)
        or str(existing.get("id", "")) != target
        or str(existing.get("slug") or existing.get("draft_slug") or "")
        != slug
    ):
        raise SubstackRepairRefused(
            "Substack draft does not match protected live identity"
        )
    title = str(
        existing.get("draft_title") or existing.get("title") or ""
    ).strip()
    bylines = existing.get("draft_bylines")
    if not isinstance(bylines, list) or not bylines:
        post_bylines = existing.get("postBylines", [])
        owned = {
            int(item["user_id"])
            for item in post_bylines
            if isinstance(item, dict)
            and str(item.get("user_id", "")).isdigit()
        }
        bylines = (
            [{"id": owned_user_id, "is_guest": False}]
            if owned == {owned_user_id}
            else []
        )
    if not title or not isinstance(bylines, list) or not bylines:
        raise SubstackRepairRefused(
            "Substack protected title/byline is missing"
        )
    byline_ids = {
        int(item["id"])
        for item in bylines
        if isinstance(item, dict)
        and str(item.get("id", "")).isdigit()
    }
    if byline_ids != {owned_user_id}:
        raise SubstackRepairRefused(
            "Substack protected byline does not match owned publication"
        )
    media = state.get("media", {})
    headline = media.get("headline_image", {})
    body_assets = [
        item
        for item in media.get("body_assets", [])
        if isinstance(item, dict)
    ]
    if not body_assets:
        raise SubstackRepairRefused(
            "Substack repair has no immutable body media"
        )
    for item in [headline, *body_assets]:
        path = Path(str(item.get("path", "")))
        if not path.is_file() or sha256(path) != item.get("sha256"):
            raise SubstackRepairRefused(
                "immutable Substack media is missing or changed"
            )
    publication = _publication()
    cookie = _cookie()
    headline_url = upload_image(
        str(headline["path"]), publication, cookie
    )
    body_urls = [
        upload_image(str(item["path"]), publication, cookie)
        for item in body_assets
    ]
    language = pair.rsplit("/", 1)[1]
    payload = _paid_payload_builder()(
        title=title,
        subtitle=str(
            existing.get("draft_subtitle")
            or existing.get("subtitle")
            or ""
        ),
        markdown=adapt_body(
            state, language, headline_url, body_urls
        ),
        byline_id=owned_user_id,
    )
    updated = _request(
        "PUT", f"/api/v1/drafts/{target}", payload=payload
    )
    if (
        not isinstance(updated, dict)
        or str(updated.get("id", "")) != target
        or str(updated.get("slug") or updated.get("draft_slug") or "")
        != slug
    ):
        raise SubstackRepairRefused(
            "Substack update changed or lost the protected ID/slug"
        )
    published = _request(
        "POST",
        f"/api/v1/drafts/{target}/publish",
        payload={"send": False, "share_automatically": False},
    )
    if (
        not isinstance(published, dict)
        or str(published.get("id", target)) != target
        or str(
            published.get("slug")
            or published.get("draft_slug")
            or slug
        )
        != slug
    ):
        raise SubstackRepairRefused(
            "Substack republish changed the protected ID/slug"
        )
    return _guard("reconcile", pair)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--pair",
        required=True,
        choices=("substack/ja", "substack/en"),
    )
    args = parser.parse_args()
    lang = args.pair.rsplit("/", 1)[1]
    cookie = os.environ.get(f"SUBSTACK_SESSION_COOKIE_{lang.upper()}", "").strip()
    if not cookie and lang == "ja":
        cookie = os.environ.get("SUBSTACK_SESSION_COOKIE", "").strip()
    if not cookie:
        raise SubstackRepairRefused(
            f"SUBSTACK_SESSION_COOKIE_{lang.upper()} is required for managed Substack publication"
        )
    os.environ["SUBSTACK_SESSION_COOKIE"] = cookie
    publication = os.environ.get(f"SUBSTACK_PUBLICATION_{lang.upper()}", "").strip().lower()
    if not publication:
        raise SubstackRepairRefused(
            f"SUBSTACK_PUBLICATION_{lang.upper()} is required; refusing implicit Substack identity"
        )
    if lang == "en":
        japanese_publication = os.environ.get("SUBSTACK_PUBLICATION_JA", "").strip().lower()
        if not japanese_publication or publication == japanese_publication:
            raise SubstackRepairRefused(
                "English Substack publication must be distinct from Japanese publication"
            )
    state_path = Path(os.environ.get("ARTICLE_PUBLICATION_STATE", ""))
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    expected_identity = str(state.get("destination_identities", {}).get(args.pair, "")).strip().lower()
    if not expected_identity or expected_identity != publication:
        raise SubstackRepairRefused(
            f"Substack identity does not match persisted state for {args.pair}"
        )
    os.environ["SUBSTACK_PUBLICATION"] = publication
    # Fail-closed PII gate: this repairs/republishes a LIVE article, so re-scan the frozen
    # artifacts before touching it. An unset ARTICLE_RUN_DIR is itself a refusal.
    gate_run_dir("substack-inplace", os.environ.get("ARTICLE_RUN_DIR", ""), pair=args.pair)
    print(
        json.dumps(
            repair(args.pair),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        SubstackRepairRefused,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        raise SystemExit(2)
