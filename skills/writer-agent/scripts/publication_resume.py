#!/usr/bin/env python3
"""Run-scoped, crash-safe publication intent and receipt store.

The store is the single source of truth for irreversible article publication.  It uses the
same fcntl + atomic-replace pattern as zenn-deferred-control.py, generalized to all eight
platform/language pairs.  Publisher code must call ``guard`` before its live side effect and
``record_live`` only after a remote reality readback.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(SCRIPT_DIR))
SUBSTACK_HTTP_DIR = SCRIPT_DIR / "substack-publish"
if str(SUBSTACK_HTTP_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(SUBSTACK_HTTP_DIR))


def _ensure_media_capable_interpreter() -> None:
    """Re-exec through the pinned Pillow-capable interpreter when needed.

    The daily agent invokes this CLI from varied interpreters (browser venvs,
    sandbox defaults); on 2026-07-25 an init call crashed inside
    media_integrity before any publication state existed because the calling
    interpreter lacked Pillow, stranding the whole run. The CLI now
    deterministically re-execs itself via the same Homebrew interpreter the
    workers already pin instead of crashing.
    """
    try:
        import PIL  # noqa: F401
    except ModuleNotFoundError:
        pinned = os.environ.get(
            "ARTICLE_MEDIA_PYTHON",
            os.environ.get("WRITER_BROWSER_PYTHON", os.sys.executable),
        )
        if (
            os.path.exists(pinned)
            and os.path.realpath(os.sys.executable)
            != os.path.realpath(pinned)
            and os.environ.get("ARTICLE_MEDIA_REEXEC") != "1"
        ):
            os.environ["ARTICLE_MEDIA_REEXEC"] = "1"
            os.execv(
                pinned,
                [pinned, os.path.abspath(__file__), *os.sys.argv[1:]],
            )
        raise


_ensure_media_capable_interpreter()

from media_integrity import (
    MAX_ASPECT_RATIO_DELTA,
    MAX_DHASH_DISTANCE,
    NOTE_EYECATCH_RATIO_WINDOW,
    X_COVER_RATIO_WINDOW,
    center_crop_content_proof,
    content_proof,
    descriptor_from_file,
    dhash_distance,
)
from canonical_media import MediaContractError, validate_media_contract
from media_create_once import MediaCreateRefused, verify as verify_media_create_once
from publication_contract import (
    ACTIVE_PAIRS,
    DORMANT_PAIRS,
    LEGACY_EXACT8_PAIRS,
    SUPPORTED_PAIRS,
)
from substack_http import bytes_request as substack_bytes_request
from publication_contract_resolver import (
    ACTIVE_ALIAS,
    PublicationContractError,
    infer_publication_contract,
)
from writer_report_worker import telegram_api_transport

# Compatibility name for callers that mean the current required set.  Legacy
# exact-eight state is selected explicitly from its persisted contract below.
REQUIRED_PAIRS = ACTIVE_PAIRS
HISTORICAL_DORMANT_STATUSES = {
    "intent",
    "live",
    "ambiguous",
    "unavailable",
    "repair-required",
    "terminal-invalid",
}
TARGET_KINDS = {
    "note/ja": "note-key",
    "zenn-article/ja": "zenn-slug",
    "devto/en": "devto-article-id",
    "substack/ja": "substack-draft-id",
    "substack/en": "substack-draft-id",
    "x-article/ja": "x-draft-url",
    "x-article/en": "x-draft-url",
    "x-post/ja": "x-post-slot",
}
PAIR_HOSTS = {
    "note/ja": {"note.com", "www.note.com"},
    "zenn-article/ja": {"zenn.dev", "www.zenn.dev"},
    "devto/en": {"dev.to", "www.dev.to"},
    "substack/ja": {"substack.com"},
    "substack/en": {"substack.com"},
    "x-article/ja": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
    "x-article/en": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
    "x-post/ja": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
}
DESTINATION_IDENTITY_ENV = {
    "note/ja": "NOTE_URLNAME",
    "zenn-article/ja": "ZENN_ACCOUNT",
    "devto/en": "DEVTO_ACCOUNT_HANDLE",
    "substack/ja": "SUBSTACK_PUBLICATION_JA",
    "substack/en": "SUBSTACK_PUBLICATION_EN",
    "x-article/ja": "X_ACCOUNT_HANDLE",
    "x-article/en": "X_ACCOUNT_HANDLE",
    "x-post/ja": "X_ACCOUNT_HANDLE",
}
IDENTITY_CONFLICT_REASON = "substack-publication-identity-conflict"

# A legacy run may contain an audit-only ledger row for a pair that never got
# past staging.  This is deliberately an allowlist: a quarantine transition
# must never treat an unfamiliar field as proof that no external effect
# occurred.  In particular, provider_response/post_id/url-like fields are
# effect-capable even when the older boolean fields still say ``published``
# false.
_NO_EFFECT_LEDGER_KEYS = frozenset(
    {
        "ts",
        "run_id",
        "topic_id",
        "topic",
        "localized_topic",
        "topic_source",
        "editorial_form",
        "platform",
        "lang",
        "pair",
        "draft_url",
        "state",
        "published",
        "verified_logged_in",
        "live_url",
        "public_id",
        "receipt",
        "published_at",
        "reality_gate",
        "error",
    }
)
_NO_EFFECT_REQUIRED_KEYS = frozenset(
    {
        "run_id",
        "topic_id",
        "platform",
        "lang",
        "state",
        "published",
        "verified_logged_in",
    }
)
_NO_EFFECT_STATE_RE = re.compile(
    r"^(?:staged(?::[a-z0-9]+(?:-[a-z0-9]+)*)?|unavailable(?::[a-z0-9]+(?:-[a-z0-9]+)*)?)$"
)
_NO_EFFECT_STATE_FORBIDDEN_TOKENS = frozenset(
    {"live", "publish", "published", "effect", "receipt", "post", "url"}
)


def _is_no_effect_ledger_row(row: dict[str, Any], state: dict[str, Any]) -> bool:
    """Return true only for the exact, effect-free legacy audit schema."""

    if not _NO_EFFECT_REQUIRED_KEYS <= row.keys():
        return False
    if set(row) - _NO_EFFECT_LEDGER_KEYS:
        return False
    state_value = row.get("state")
    if not isinstance(state_value, str) or not _NO_EFFECT_STATE_RE.fullmatch(state_value):
        return False
    state_lower = state_value.lower()
    if any(token in state_lower for token in _NO_EFFECT_STATE_FORBIDDEN_TOKENS):
        return False
    if (
        row.get("run_id") != state.get("run_id")
        or row.get("topic_id") != state.get("topic_id")
        or row.get("platform") != "substack"
        or row.get("lang") != "en"
        or row.get("published") is not False
        or row.get("verified_logged_in") is not False
    ):
        return False
    if any(row.get(key) not in {None, ""} for key in ("live_url", "public_id", "receipt", "published_at", "effect", "readback")):
        return False
    return row.get("reality_gate") in {None, ""}
X_POST_MAX_CHARS = 280


def _rule_evidence_shapes(rule: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """Accepted (identity_source, source) pairs for one recovery rule."""
    shapes = rule.get("evidence_shapes")
    if shapes:
        return tuple((str(a), str(b)) for a, b in shapes)
    return ((str(rule["identity_source"]), str(rule["source"])),)


def _rule_error_matches(rule: dict[str, Any], value: Any) -> bool:
    """Match only listed recovery errors or explicitly bounded legacy prefixes."""
    if not isinstance(value, str):
        return False
    if value in rule.get("errors", ()):
        return True
    return any(
        value.startswith(str(prefix))
        for prefix in rule.get("error_prefixes", ())
    )


class InvariantError(ValueError):
    """A stable identity or run boundary is missing/contradictory."""


class ResumeExhausted(RuntimeError):
    """The bounded full-invocation resume budget is exhausted."""


def configured_destination_identities(
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Resolve protected identities without allowing JA/EN Substack conflation."""

    values = os.environ if environ is None else environ
    identities: dict[str, str] = {}
    for pair, key in DESTINATION_IDENTITY_ENV.items():
        identity = values.get(key, "").strip().lstrip("@").lower()
        if not identity:
            raise InvariantError(f"{key} is required for Writer initialization")
        identities[pair] = identity
    japanese = identities["substack/ja"]
    english = identities["substack/en"]
    if japanese == english:
        raise InvariantError(
            "English Substack publication must be distinct from Japanese publication"
        )
    for pair, identity in (("substack/ja", japanese), ("substack/en", english)):
        if not identity.endswith(".substack.com") or "/" in identity:
            raise InvariantError(f"invalid Substack publication identity for {pair}")
        identities[pair] = identity
    validate_destination_identities(identities)
    return identities


def _validate_identity(pair: str, identity: Any) -> None:
    if not isinstance(identity, str) or not identity.strip():
        raise InvariantError(f"destination identity is missing for {pair}")
    value = identity.strip().lower()
    if pair in {"substack/ja", "substack/en"}:
        if not value.endswith(".substack.com") or "/" in value:
            raise InvariantError(f"invalid Substack publication identity for {pair}")
    elif re.fullmatch(r"[a-z0-9_-]+", value) is None:
        raise InvariantError(f"invalid destination identity for {pair}")


def validate_destination_identities(identities: Any) -> None:
    """Validate persisted identity shape before any target or publisher action."""

    if not isinstance(identities, dict):
        raise InvariantError("destination identities must be an object")
    if set(identities) != set(DESTINATION_IDENTITY_ENV):
        raise InvariantError("destination identities have an unexpected pair set")
    for pair, identity in identities.items():
        _validate_identity(pair, identity)
    japanese = identities["substack/ja"].strip().lower()
    english = identities["substack/en"].strip().lower()
    if any(
        not value.endswith(".substack.com") or "/" in value
        for value in (japanese, english)
    ):
        raise InvariantError("invalid Substack publication identity")
    if japanese == english:
        raise InvariantError(
            "persisted English Substack identity must differ from Japanese identity"
        )


def validate_persisted_destination_identities(state: dict[str, Any]) -> None:
    """Validate persisted identities, allowing one explicit legacy quarantine.

    A pre-gate active-four run could freeze JA and EN to the same Substack host.
    That state is unsafe for EN publication, but it must not hold unrelated
    destinations hostage once the EN pair is durably quarantined.  The
    quarantine is deliberately narrow: the persisted identities remain
    immutable, the EN pair must be unavailable with no receipt, and the
    quarantine record must bind the exact old identity and reason.
    """

    identities = state.get("destination_identities")
    try:
        validate_destination_identities(identities)
        return
    except InvariantError:
        pass

    if not isinstance(identities, dict):
        raise InvariantError("destination identities must be an object")
    if set(identities) != set(DESTINATION_IDENTITY_ENV):
        raise InvariantError("destination identities have an unexpected pair set")
    japanese = identities.get("substack/ja")
    english = identities.get("substack/en")
    quarantine = state.get("identity_quarantine")
    en_entry = state.get("pairs", {}).get("substack/en")
    if not (
        isinstance(japanese, str)
        and isinstance(english, str)
        and japanese.strip().lower() == english.strip().lower()
        and isinstance(quarantine, dict)
        and quarantine.get("version") == 1
        and quarantine.get("pair") == "substack/en"
        and quarantine.get("reason") == IDENTITY_CONFLICT_REASON
        and quarantine.get("previous_identity") == english.strip().lower()
        and isinstance(quarantine.get("recorded_at"), str)
        and isinstance(en_entry, dict)
        and en_entry.get("status") == "unavailable"
        and en_entry.get("error") == IDENTITY_CONFLICT_REASON
        and not en_entry.get("receipt")
    ):
        raise InvariantError(
            "persisted destination identities are invalid or unquarantined"
        )
    # The non-Substack identities still have to remain the protected account
    # identities.  Only the explicitly quarantined equal Substack pair is
    # tolerated here; a malformed sibling must fail closed.
    for pair, identity in identities.items():
        if pair in {"substack/ja", "substack/en"}:
            continue
        _validate_identity(pair, identity)
    if any(
        not isinstance(value, str)
        or not value.strip().lower().endswith(".substack.com")
        or "/" in value
        for value in (japanese, english)
    ):
        raise InvariantError("invalid quarantined Substack publication identity")


def validate_x_post_text(path: Path) -> int:
    """Use the same character contract as the live X publisher before freeze."""
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise InvariantError(f"x post is unreadable: {error}") from error
    measured = len(value)
    if measured < 1 or measured > X_POST_MAX_CHARS:
        raise InvariantError(
            f"X Post must contain 1..{X_POST_MAX_CHARS} characters "
            f"before publication init (measured {measured})"
        )
    return measured


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require_quality_terminals(
    run_dir: Path,
    drafts: dict[str, Path],
) -> dict[str, dict[str, Any]]:
    """Require hash-bound editorial, reader, and safety PASS for both drafts."""

    receipts: dict[str, dict[str, Any]] = {}
    quality_self_heal = None
    try:
        quality_self_heal = json.loads(
            (Path(run_dir) / "gates" / "quality-self-heal.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError, TypeError):
        quality_self_heal = None
    force_advisory = False
    if isinstance(quality_self_heal, dict):
        try:
            from quality_self_heal import validate_force_receipt

            force_advisory = validate_force_receipt(
                Path(run_dir), drafts
            )
        except Exception:
            force_advisory = False
    advisory_seen = False
    for lang in ("ja", "en"):
        draft = Path(drafts[lang])
        path = Path(run_dir) / "gates" / f"quality-terminal-{lang}.json"
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as error:
            raise InvariantError(
                f"{lang} quality terminal receipt is missing or malformed"
            ) from error
        continuous = os.environ.get("ARTICLE_PUBLICATION_POLICY") == "continuous"
        expected = {
            "status": "terminal",
            "lang": lang,
            "article_sha256": sha256(draft),
            "identity_gate": "PASS",
            "safety_gate": "ALLOW",
        }
        if not isinstance(receipt, dict) or any(
            receipt.get(key) != value for key, value in expected.items()
        ):
            raise InvariantError(
                f"{lang} quality terminal does not match the current artifact"
            )
        allowed_quality = {"PASS", "ADVISORY"} if continuous else {"PASS"}
        if (
            receipt.get("editorial_gate") not in allowed_quality
            or receipt.get("reader_gate") not in allowed_quality
        ):
            raise InvariantError(
                f"{lang} quality terminal does not match the current artifact"
            )
        advisory_seen = advisory_seen or any(
            receipt.get(key) == "ADVISORY"
            for key in ("editorial_gate", "reader_gate")
        )
        receipts[lang] = {
            key: receipt[key]
            for key in (*expected, "editorial_gate", "reader_gate")
        }
    if advisory_seen and not force_advisory:
        raise InvariantError(
            "editorial/reader advisory requires single-evaluation force receipt"
        )
    return receipts


def _require_cta(run_dir: Path, artifacts: dict[str, Path]) -> None:
    """Fail closed before publication state exists if any frozen artifact has no door."""
    gate = SCRIPT_DIR / "cta-gate.sh"
    gates_dir = run_dir / "gates"
    gates_dir.mkdir(exist_ok=True)
    artifact_ids = {
        "ja": "article-ja",
        "en": "article-en",
        "x-post-ja": "x-post-ja",
    }
    click_ids: list[str] = []
    for name, path in artifacts.items():
        completed = subprocess.run(
            [
                "bash",
                str(gate),
                str(path),
                "--run-id",
                run_dir.name,
                "--artifact-id",
                artifact_ids[name],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            verdict = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise InvariantError(
                f"{name} CTA gate returned malformed evidence"
            ) from exc
        evidence_path = gates_dir / f"cta-{name}.json"
        evidence_path.write_text(
            json.dumps(verdict, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        if completed.returncode != 0 or verdict.get("verdict") != "PASS":
            reason = str(verdict.get("reason", "cta-gate-failed"))
            raise InvariantError(f"{name} CTA invariant failed: {reason}")
        click_ids.append(str(verdict["click_id"]))
    if len(click_ids) != len(set(click_ids)):
        raise InvariantError("CTA click_id must be unique per artifact")


def _validate_devto_frontmatter(source: str) -> None:
    match = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", source, re.S)
    if not match:
        raise InvariantError(
            "English canonical draft requires Dev.to frontmatter before "
            "publication-state initialization"
        )
    lines = match.group(1).splitlines()
    title = ""
    tags: list[str] = []
    for index, line in enumerate(lines):
        field = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*)$", line)
        if not field:
            continue
        key, raw_value = field.groups()
        value = raw_value.strip().strip("\"'")
        if key.lower() == "title":
            title = value
        elif key.lower() == "tags":
            tags.extend(
                item.strip().strip("\"'")
                for item in value.strip("[]").split(",")
                if item.strip()
            )
            if not value:
                for nested in lines[index + 1 :]:
                    if re.match(r"^[A-Za-z][A-Za-z0-9_-]*:\s*", nested):
                        break
                    item = re.match(r"^\s+-\s+(.+?)\s*$", nested)
                    if item:
                        tags.append(item.group(1).strip().strip("\"'"))
    if not title or not tags:
        raise InvariantError(
            "English canonical draft requires non-empty Dev.to frontmatter "
            "title and tags before publication-state initialization"
        )


def valid_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_nonpublication_quality_audit(
    row: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    """A failed-quality audit records no destination or remote side effect."""
    return (
        row.get("topic_id") == state.get("topic_id")
        and row.get("platform") == "quality"
        and row.get("lang") == "ja+en"
        and row.get("published") is False
        and row.get("verified_logged_in") is False
        and isinstance(row.get("state"), str)
        and row["state"].startswith("carry-over:quality-block:")
        and not row.get("draft_url")
        and not row.get("live_url")
        and not row.get("receipt")
        and not row.get("public_id")
        and not row.get("published_at")
    )


def is_self_owned_publication_receipt(
    row: dict[str, Any], state: dict[str, Any]
) -> bool:
    """Recognize a strict adjunct receipt without expanding the exact8 set."""
    live_url = row.get("live_url")
    parsed = urlparse(live_url) if isinstance(live_url, str) else None
    configured_base = urlparse(str(state.get("self_owned_base_url", "")))
    lang = row.get("lang")
    return (
        row.get("run_id") == state.get("run_id")
        and row.get("topic_id") == state.get("topic_id")
        and row.get("platform") == "self-owned"
        and lang in {"ja", "en"}
        and row.get("published") is True
        and row.get("reality_gate") == "PASS"
        and row.get("verified") is True
        and parsed is not None
        and parsed.scheme == "https"
        and configured_base.scheme == "https"
        and bool(configured_base.hostname)
        and parsed.netloc.lower() == configured_base.netloc.lower()
        and re.fullmatch(
            rf"{re.escape(configured_base.path.rstrip('/'))}/blog/[a-z0-9][a-z0-9-]{{0,99}}",
            parsed.path,
        ) is not None
        and row.get("artifact_id") == f"{state.get('run_id')}__self-owned__{lang}"
        and all(
            re.fullmatch(r"[0-9a-f]{64}", str(row.get(field, ""))) is not None
            for field in (
                "artifact_sha256", "preview_sha256", "paid_sha256"
            )
        )
    )


def _self_owned_base_from_rows(
    rows: list[dict[str, Any]], state: dict[str, Any]
) -> str:
    bases: set[str] = set()
    for row in rows:
        if (
            row.get("run_id") != state.get("run_id")
            or row.get("topic_id") != state.get("topic_id")
            or row.get("platform") != "self-owned"
            or row.get("published") is not True
        ):
            continue
        parsed = urlparse(str(row.get("live_url", "")))
        match = re.fullmatch(r"(.*)/blog/[a-z0-9][a-z0-9-]{0,99}", parsed.path)
        if parsed.scheme != "https" or not parsed.hostname or match is None:
            raise InvariantError("legacy self-owned receipt URL is invalid")
        bases.add(f"https://{parsed.netloc.lower()}{match.group(1).rstrip('/')}")
    if len(bases) > 1:
        raise InvariantError("legacy self-owned receipts disagree on base URL")
    return next(iter(bases), "")


def _configured_self_owned_base_url() -> str:
    raw = os.environ.get("ARTICLE_SELF_OWNED_BASE_URL", "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise InvariantError("ARTICLE_SELF_OWNED_BASE_URL must be an absolute HTTPS URL")
    return f"https://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def validate_target(pair: str, kind: str, target: str) -> None:
    if pair not in SUPPORTED_PAIRS or TARGET_KINDS[pair] != kind:
        raise InvariantError(f"target kind {kind!r} is invalid for {pair!r}")
    if not isinstance(target, str) or not target:
        raise InvariantError("stable target is empty")
    if kind == "note-key" and re.fullmatch(r"[A-Za-z0-9_-]{4,200}", target) is None:
        raise InvariantError("invalid note key")
    if kind == "zenn-slug" and re.fullmatch(r"[a-z0-9_-]{12,50}", target) is None:
        raise InvariantError("invalid Zenn slug")
    if kind == "substack-draft-id" and re.fullmatch(r"[0-9]{1,30}", target) is None:
        raise InvariantError("invalid Substack draft id")
    if kind == "devto-article-id" and re.fullmatch(r"[0-9]{1,30}", target) is None:
        raise InvariantError("invalid Dev.to article id")
    if kind == "x-post-slot" and re.fullmatch(
        r"(?:daily-[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{8}-[0-9]{6})",
        target,
    ) is None:
        raise InvariantError("invalid X Post slot identity")
    if kind == "x-draft-url":
        parsed = urlparse(target)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {"x.com", "twitter.com"}:
            raise InvariantError("invalid X draft URL")


def _valid_pair_host(pair: str, live_url: str) -> bool:
    try:
        parsed = urlparse(live_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if pair.startswith("substack/"):
        allowed = host == "substack.com" or host.endswith(".substack.com")
    else:
        allowed = host in PAIR_HOSTS[pair]
    return parsed.scheme == "https" and allowed


def _expected_artifact_hash(state: dict[str, Any], pair: str) -> str:
    if pair == "x-post/ja":
        return str(state.get("x_post", {}).get("sha256", ""))
    lang = str(state.get("pairs", {}).get(pair, {}).get("lang", ""))
    return str(state.get("drafts", {}).get(lang, {}).get("sha256", ""))


def _expected_asset_hashes(state: dict[str, Any], pair: str) -> list[str]:
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
        headline = media.get("headline_image", {})
        descriptors.insert(0, dict(headline) if isinstance(headline, dict) else {})
    return descriptors


def fetch_remote_asset(url: str, _expected: dict[str, Any]) -> bytes:
    expected_path = Path(str(_expected.get("path", ""))).resolve(strict=False)
    if (
        os.environ.get("ARTICLE_TEST_ONLY") == "1"
        and (urlparse(url).hostname or "").lower() == "assets.example"
        and str(expected_path).startswith(("/tmp/", "/private/tmp/"))
    ):
        return expected_path.read_bytes()
    host = (urlparse(url).hostname or "").lower()
    if host in {"substack-post-media.s3.amazonaws.com", "substackcdn.com"}:
        data, content_type = substack_bytes_request(
            url, {"User-Agent": "writer-agent/1"}, timeout=30
        )
        if len(data) > 25 * 1024 * 1024:
            raise InvariantError("public asset exceeds verification limit")
        if not content_type.startswith("image/"):
            raise InvariantError("public asset readback is not an image")
        return data
    request = urllib.request.Request(url, headers={"User-Agent": "writer-agent/1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        content_type = str(response.headers.get("Content-Type", "")).lower()
        data = response.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise InvariantError("public asset exceeds verification limit")
    if not content_type.startswith("image/"):
        raise InvariantError("public asset readback is not an image")
    return data


def _validate_asset_proofs(
    state: dict[str, Any],
    pair: str,
    evidence: dict[str, Any],
    *,
    reread_remote_assets: bool = True,
) -> None:
    expected = _expected_asset_descriptors(state, pair)
    proofs = evidence.get("asset_proofs")
    if not isinstance(proofs, list) or len(proofs) != len(expected):
        raise InvariantError("receipt public asset content proof is incomplete")
    for asset_index, (descriptor, proof) in enumerate(zip(expected, proofs)):
        if not isinstance(proof, dict):
            raise InvariantError("receipt public asset content proof is malformed")
        if (
            proof.get("expected_sha256") != descriptor.get("sha256")
            or not valid_http_url(proof.get("remote_url"))
        ):
            raise InvariantError("receipt public asset content proof conflicts with state")
        method = proof.get("match_method")
        if method == "exact-sha256":
            if proof.get("remote_sha256") != descriptor.get("sha256"):
                raise InvariantError("receipt exact public asset hash does not match")
        elif method == "visual-dhash":
            expected_dhash = descriptor.get("dhash")
            remote_dhash = proof.get("remote_dhash")
            try:
                distance = dhash_distance(expected_dhash, remote_dhash)
                expected_ratio = int(descriptor["width"]) / int(descriptor["height"])
                remote_ratio = int(proof["remote_width"]) / int(proof["remote_height"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
                raise InvariantError("receipt visual public asset proof is malformed") from error
            if (
                proof.get("expected_dhash") != expected_dhash
                or proof.get("dhash_distance") != distance
                or distance > MAX_DHASH_DISTANCE
                or (
                    abs(expected_ratio - remote_ratio) / expected_ratio
                    > MAX_ASPECT_RATIO_DELTA
                )
            ):
                raise InvariantError("receipt visual public asset proof does not match")
        elif method == "visual-center-crop-dhash":
            # Both X's wide cover and note's 1280x670 eyecatch are center
            # crops of the same immutable source; comparing a crop against
            # the full image with plain dHash measured distance 15 on the
            # live 07-25 note cover (2026-07-25).
            if asset_index != 0 or not (
                pair.startswith("x-article/") or pair == "note/ja"
            ):
                raise InvariantError(
                    "receipt center-crop proof is not a supported cover"
                )
            crop_window = (
                NOTE_EYECATCH_RATIO_WINDOW
                if pair == "note/ja"
                else X_COVER_RATIO_WINDOW
            )
            try:
                horizontal = int(proof["dhash_distance"])
                vertical = int(proof["vertical_dhash_distance"])
                remote_ratio = int(proof["remote_width"]) / int(
                    proof["remote_height"]
                )
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
                raise InvariantError(
                    "receipt center-crop proof is malformed"
                ) from error
            if (
                proof.get("expected_dhash") != descriptor.get("dhash")
                or horizontal > MAX_DHASH_DISTANCE
                or vertical > MAX_DHASH_DISTANCE
                or not crop_window[0] <= remote_ratio <= crop_window[1]
            ):
                raise InvariantError(
                    "receipt center-crop proof does not match"
                )
        else:
            raise InvariantError("receipt public asset proof method is invalid")
        if not reread_remote_assets:
            # The irreversible publication boundary already minted and
            # re-read this proof. Resume planning is local bookkeeping: doing
            # public network I/O here makes one slow CDN hold every run.
            continue
        try:
            remote_bytes = fetch_remote_asset(
                str(proof["remote_url"]), descriptor
            )
            if method == "visual-center-crop-dhash":
                # Re-prove with the same window the proof was minted under;
                # X's default window recomputed None for note's 1.91:1
                # eyecatch and refused a receipt whose probe had just passed.
                fresh = center_crop_content_proof(
                    descriptor,
                    remote_bytes,
                    str(proof["remote_url"]),
                    ratio_window=crop_window,
                )
            else:
                fresh = content_proof(
                    descriptor, remote_bytes, str(proof["remote_url"])
                )
        except Exception as error:
            raise InvariantError("receipt public asset content proof cannot be re-read") from error
        comparable = (
            "expected_sha256",
            "remote_sha256",
            "remote_url",
            "match_method",
            "expected_dhash",
            "remote_dhash",
            "dhash_distance",
            "expected_width",
            "expected_height",
            "remote_width",
            "remote_height",
        )
        if method == "visual-center-crop-dhash":
            comparable = (
                *comparable,
                "expected_crop_dhash",
                "expected_crop_vertical_dhash",
                "remote_vertical_dhash",
                "vertical_dhash_distance",
                "expected_crop_box",
            )
        if fresh is None or any(fresh.get(key) != proof.get(key) for key in comparable):
            raise InvariantError("receipt public asset content proof is not reproducible")
    if evidence.get("asset_urls") != [proof["remote_url"] for proof in proofs]:
        raise InvariantError("receipt public media URLs conflict with content proofs")


def _remote_identity_verified(
    state: dict[str, Any], pair: str, remote: dict[str, Any]
) -> bool:
    expected = state.get("destination_identities", {}).get(pair)
    return bool(
        isinstance(expected, str)
        and expected.strip()
        and remote.get("destination_identity") == expected
        and remote.get("identity_verified") is True
        and isinstance(remote.get("identity_source"), str)
        and remote["identity_source"].strip()
    )


def validate_receipt_evidence(
    state: dict[str, Any],
    pair: str,
    live_url: str,
    evidence: dict[str, Any],
    *,
    reread_remote_assets: bool = True,
) -> None:
    """Require the donor-proven public identity/content/media readback shape."""
    entry = state.get("pairs", {}).get(pair, {})
    if evidence.get("verified") is not True:
        raise InvariantError("receipt requires verified remote readback")
    if not _valid_pair_host(pair, live_url):
        raise InvariantError(f"receipt live URL host is invalid for {pair}")
    public_id = evidence.get("public_id")
    if not isinstance(public_id, str) or not public_id.strip():
        raise InvariantError("receipt requires a stable public ID")
    raw_published_at = evidence.get("published_at")
    if not isinstance(raw_published_at, str):
        raise InvariantError("receipt requires a remote publication timestamp")
    try:
        published_at = datetime.fromisoformat(
            raw_published_at.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise InvariantError("receipt published_at is not ISO8601") from error
    if (
        published_at.tzinfo is None
        or published_at.utcoffset() is None
        or published_at.year < 2020
        or published_at.astimezone(timezone.utc)
        > datetime.now(timezone.utc) + timedelta(minutes=10)
    ):
        raise InvariantError("receipt published_at is implausible")
    if evidence.get("stable_target") != entry.get("target"):
        raise InvariantError("receipt stable target conflicts with publication intent")
    if evidence.get("artifact_sha256") != _expected_artifact_hash(state, pair):
        raise InvariantError("receipt artifact hash conflicts with immutable artifact")
    if evidence.get("language") != entry.get("lang"):
        raise InvariantError("receipt language conflicts with publication intent")
    if evidence.get("content_verified") is not True:
        raise InvariantError("receipt did not verify canonical content")
    if not _remote_identity_verified(state, pair, evidence):
        raise InvariantError("receipt destination identity is not verified")
    expected_assets = _expected_asset_hashes(state, pair)
    if evidence.get("asset_hashes") != expected_assets:
        raise InvariantError("receipt asset hashes conflict with immutable media")
    if pair != "x-post/ja" and evidence.get("asset_verified") is not True:
        raise InvariantError("receipt did not verify every public media asset")
    _validate_asset_proofs(
        state,
        pair,
        evidence,
        reread_remote_assets=reread_remote_assets,
    )
    if pair != "x-post/ja":
        asset_urls = evidence.get("asset_urls")
        if (
            not isinstance(asset_urls, list)
            or any(
                not isinstance(url, str) or not valid_http_url(url)
                for url in asset_urls
            )
        ):
            raise InvariantError("receipt requires canonical public media URLs")
        if len(set(asset_urls)) < len(expected_assets):
            raise InvariantError("receipt public media evidence is incomplete")
    required_flags = {
        "note/ja": ("eyecatch_verified", "body_media_verified"),
        "zenn-article/ja": ("body_media_verified",),
        "devto/en": ("body_media_verified",),
        "substack/ja": ("body_media_verified",),
        "substack/en": ("body_media_verified",),
        "x-article/ja": ("cover_verified", "body_media_verified"),
        "x-article/en": ("cover_verified", "body_media_verified"),
        "x-post/ja": ("timeline_verified", "emoji_verified"),
    }[pair]
    if any(evidence.get(flag) is not True for flag in required_flags):
        raise InvariantError("receipt is missing destination media/readback proof")
    if pair == "x-post/ja":
        status_id = evidence.get("status_id")
        if (
            status_id != public_id
            or not isinstance(status_id, str)
            or not status_id.isascii()
            or not status_id.isdigit()
            or not live_url.rstrip("/").endswith(f"/status/{status_id}")
        ):
            raise InvariantError("receipt X Post status ID is invalid")


class PublicationStore:
    def __init__(self, state_path: Path, ledger_path: Path):
        self.state_path = Path(state_path)
        self.ledger_path = Path(ledger_path)
        self.backup_path = self.state_path.with_name(f"{self.state_path.name}.bak")
        self.lock_path = self.state_path.with_name(f".{self.state_path.name}.lock")

    @staticmethod
    def _is_legacy_state(state: dict[str, Any]) -> bool:
        """Recognize the shared explicit or validated pre-migration contract."""
        try:
            return infer_publication_contract(state) == "legacy-exact8"
        except PublicationContractError:
            return False

    @staticmethod
    def _is_active_alias_state(state: dict[str, Any]) -> bool:
        """Recognize only the explicit pre-active-four marker."""
        return state.get("publication_contract") == ACTIVE_ALIAS

    @classmethod
    def _required_pairs_for_state(cls, state: dict[str, Any]) -> tuple[str, ...]:
        """Return the persisted contract without treating dormant skips as work.

        New states use active-four. Persisted explicit legacy state remains
        resumable as exact-eight so an already-owned publication is not dropped.
        """
        if cls._is_legacy_state(state):
            return LEGACY_EXACT8_PAIRS
        return ACTIVE_PAIRS

    @classmethod
    def _assert_pair_mutation_allowed(
        cls,
        state: dict[str, Any],
        pair: str,
        *,
        allow_dormant_skip: bool = False,
    ) -> None:
        if pair not in SUPPORTED_PAIRS:
            raise InvariantError(f"unknown pair {pair}")
        if pair in DORMANT_PAIRS and not cls._is_legacy_state(state):
            current = state.get("pairs", {}).get(pair)
            if not (
                allow_dormant_skip
                and (
                    current is None
                    or (
                        isinstance(current, dict)
                        and current.get("status") == "skipped"
                    )
                )
            ):
                raise InvariantError(
                    f"{pair} is a dormant destination; use an explicit skip receipt"
                )

    @classmethod
    def _pair_set_valid(cls, state: dict[str, Any]) -> bool:
        required = cls._required_pairs_for_state(state)
        pairs = state.get("pairs", {})
        if not isinstance(pairs, dict):
            return False
        keys = set(pairs)
        if required == LEGACY_EXACT8_PAIRS:
            return keys == set(required)
        if cls._is_active_alias_state(state):
            historical = {"zenn-article/ja", "devto/en"}
            if (
                not keys <= set(SUPPORTED_PAIRS)
                or not set(required) <= keys
                or not {"x-article/en", "x-post/ja"} <= keys
            ):
                return False
            for pair in keys - set(required):
                entry = pairs[pair]
                if not isinstance(entry, dict):
                    return False
                if pair in historical and entry.get("status") in HISTORICAL_DORMANT_STATUSES:
                    continue
                receipt = entry.get("skip_receipt", {})
                if (
                    pair not in DORMANT_PAIRS
                    or entry.get("status") != "skipped"
                    or not isinstance(receipt, dict)
                    or receipt.get("type") != "dormant-destination"
                    or receipt.get("pair") != pair
                    or not receipt.get("reason")
                    or receipt.get("slo") != "not-applicable"
                    or not receipt.get("recorded_at")
                ):
                    return False
            return True
        if keys != set(SUPPORTED_PAIRS):
            return False
        if not keys <= set(SUPPORTED_PAIRS):
            return False
        for pair in keys - set(required):
            entry = pairs[pair]
            if not isinstance(entry, dict):
                return False
            receipt = entry.get("skip_receipt", {})
            if (
                pair not in DORMANT_PAIRS
                or entry.get("status") != "skipped"
                or not isinstance(receipt, dict)
                or receipt.get("type") != "dormant-destination"
                or receipt.get("pair") != pair
                or not receipt.get("reason")
                or receipt.get("slo") != "not-applicable"
                or not receipt.get("recorded_at")
            ):
                return False
        return True

    @classmethod
    def _intent_requirement_message(cls, state: dict[str, Any]) -> str:
        if cls._required_pairs_for_state(state) == LEGACY_EXACT8_PAIRS:
            return "all eight valid stable intents must exist before publication"
        return (
            "all active-four valid stable intents and dormant skip receipts "
            "must exist before publication"
        )

    def _atomic_write_path(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _write_locked(self, data: dict[str, Any]) -> None:
        data["updated_at"] = utc_now()
        self._atomic_write_path(self.state_path, data)
        # A same-generation backup makes a later torn/corrupt primary recover without losing
        # the last intent or receipt.  It is not consulted while the primary is valid.
        self._atomic_write_path(self.backup_path, data)

    @staticmethod
    def _decode(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise InvariantError(f"invalid publication state: {path}")
        return value

    def _read_locked(self) -> dict[str, Any]:
        try:
            state = self._decode(self.state_path)
        except (OSError, json.JSONDecodeError, TypeError, InvariantError) as primary_error:
            try:
                recovered = self._decode(self.backup_path)
            except (OSError, json.JSONDecodeError, TypeError, InvariantError) as backup_error:
                raise InvariantError(
                    f"publication state and backup are unreadable: {primary_error}; {backup_error}"
                ) from backup_error
            if self.state_path.exists():
                corrupt = self.state_path.with_name(
                    f"{self.state_path.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
                )
                shutil.copy2(self.state_path, corrupt)
            self._atomic_write_path(self.state_path, recovered)
            state = recovered
        return self._upgrade_legacy_self_owned_base_locked(
            state, _configured_self_owned_base_url()
        )

    def _upgrade_legacy_self_owned_base_locked(
        self, state: dict[str, Any], configured_base: str
    ) -> dict[str, Any]:
        if "self_owned_base_url" in state:
            return state
        derived_base = _self_owned_base_from_rows(self._ledger_rows_locked(), state)
        if not derived_base and not configured_base:
            return state
        if derived_base and (not configured_base or derived_base != configured_base):
            raise InvariantError(
                "legacy self-owned receipt does not match configured base URL"
            )
        state["self_owned_base_url"] = derived_base or configured_base
        self._write_locked(state)
        return state

    def _lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.lock_path.open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return lock

    def read(self) -> dict[str, Any]:
        with self._lock():
            return self._read_locked()

    @staticmethod
    def _reject_parent_traversal(path: Path, label: str) -> None:
        if ".." in Path(path).parts:
            raise InvariantError(f"{label} contains parent traversal")

    @staticmethod
    def _require_regular_file(path: Path, label: str) -> None:
        if path.is_symlink() or not path.is_file():
            raise InvariantError(f"{label} must be a regular non-symlink file")
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                raise InvariantError(f"{label} must be a regular non-symlink file")
        except OSError as error:
            raise InvariantError(f"{label} is unreadable: {error}") from error

    def _validate_layout(
        self,
        run_id: str,
        run_dir: Path,
        drafts: dict[str, Path],
        x_post: Path | None,
        headline_image: Path,
        body_assets: list[Path],
        *,
        require_state: bool,
    ) -> Path:
        """Validate the one canonical publication boundary without trusting resolve alone."""
        run_dir = Path(run_dir)
        self._reject_parent_traversal(run_dir, "run directory")
        self._reject_parent_traversal(self.state_path, "publication state path")
        self._reject_parent_traversal(self.ledger_path, "publication ledger path")
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise InvariantError("run directory must be a real directory")
        resolved_run = run_dir.resolve(strict=True)
        if not run_id or resolved_run.name != run_id:
            raise InvariantError("run id does not match canonical run directory")

        expected_state = resolved_run / "gates" / "publication-state.json"
        actual_state = self.state_path.resolve(strict=False)
        if actual_state != expected_state:
            raise InvariantError("publication state path is outside the canonical run boundary")
        if self.state_path.is_symlink():
            raise InvariantError("publication state must not be a symlink")
        if require_state:
            self._require_regular_file(self.state_path, "publication state")

        expected_ledger = resolved_run.parent.parent / "articles.jsonl"
        actual_ledger = self.ledger_path.resolve(strict=False)
        if actual_ledger != expected_ledger:
            raise InvariantError("publication ledger path is outside the canonical state boundary")
        if self.ledger_path.exists():
            self._require_regular_file(self.ledger_path, "publication ledger")

        if set(drafts) != {"ja", "en"}:
            raise InvariantError("both immutable ja/en drafts are required")
        for lang in ("ja", "en"):
            draft = Path(drafts[lang])
            self._reject_parent_traversal(draft, f"{lang} draft path")
            self._require_regular_file(draft, f"{lang} draft")
            expected_draft = resolved_run / f"article-{lang}.md"
            if draft.resolve(strict=True) != expected_draft:
                raise InvariantError(f"{lang} draft is outside the canonical run boundary")
            try:
                source = draft.read_text(encoding="utf-8")
                validate_media_contract(source)
                if lang == "en" and not require_state:
                    _validate_devto_frontmatter(source)
            except (OSError, UnicodeError, MediaContractError) as error:
                raise InvariantError(
                    f"{lang} canonical media contract is invalid; run "
                    f"canonical_media.py attach before publication init: {error}"
                ) from error
        if x_post is not None:
            self._reject_parent_traversal(x_post, "x post path")
            self._require_regular_file(x_post, "x post")
            if x_post.resolve(strict=True) != resolved_run / "x-post-ja.txt":
                raise InvariantError("x post is outside the canonical run boundary")
        self._reject_parent_traversal(headline_image, "headline image path")
        self._require_regular_file(headline_image, "headline image")
        if headline_image.resolve(strict=True) != resolved_run / "headline-image.png":
            raise InvariantError("headline image is outside the canonical run boundary")
        if not body_assets:
            raise InvariantError("at least one body media asset is required")
        resolved_assets: set[Path] = set()
        media_hashes = {sha256(headline_image)}
        for asset in body_assets:
            self._reject_parent_traversal(asset, "body media path")
            self._require_regular_file(asset, "body media")
            resolved_asset = asset.resolve(strict=True)
            if resolved_asset.parent != resolved_run or resolved_asset == headline_image.resolve(
                strict=True
            ):
                raise InvariantError("body media is outside the canonical run boundary")
            if resolved_asset in resolved_assets:
                raise InvariantError("duplicate body media asset")
            digest = sha256(resolved_asset)
            if digest in media_hashes:
                raise InvariantError("duplicate media bytes between headline and body")
            media_hashes.add(digest)
            resolved_assets.add(resolved_asset)
        if (resolved_run / "gates/media-create-required.json").exists():
            try:
                verify_media_create_once(resolved_run)
            except (OSError, MediaCreateRefused) as error:
                raise InvariantError(
                    f"media create-once boundary is invalid: {error}"
                ) from error
        return resolved_run

    def _validate_state_boundary_locked(
        self, state: dict[str, Any], expected_run_dir: Path
    ) -> None:
        validate_persisted_destination_identities(state)
        stored_drafts = {
            lang: Path(str(state.get("drafts", {}).get(lang, {}).get("path", "")))
            for lang in ("ja", "en")
        }
        stored_x_post = (
            state.get("x_post", {}).get("path")
            if self._is_legacy_state(state)
            else None
        )
        resolved_run = self._validate_layout(
            str(state.get("run_id", "")),
            expected_run_dir,
            stored_drafts,
            Path(str(stored_x_post)) if stored_x_post else None,
            Path(
                str(
                    state.get("media", {})
                    .get("headline_image", {})
                    .get("path", "")
                )
            ),
            [
                Path(str(item.get("path", "")))
                for item in state.get("media", {}).get("body_assets", [])
                if isinstance(item, dict)
            ],
            require_state=True,
        )
        if state.get("run_dir") != str(resolved_run):
            raise InvariantError("stored run directory does not match current run boundary")
        if state.get("state_path") != str(self.state_path.resolve(strict=True)):
            raise InvariantError("stored publication state path does not match current state")
        if state.get("ledger_path") != str(self.ledger_path.resolve(strict=False)):
            raise InvariantError("stored publication ledger path does not match current ledger")

    def validate_managed_boundary(self, expected_run_dir: Path) -> dict[str, Any]:
        """Bind environment, stored identity, drafts, state and ledger to one current run."""
        with self._lock():
            state = self._read_locked()
            self._validate_state_boundary_locked(state, expected_run_dir)
            return state

    def initialize(
        self,
        run_id: str,
        run_dir: Path,
        topic_id: str,
        drafts: dict[str, Path],
        safety_status: str,
        max_resume_attempts: int,
        x_post: Path | None = None,
        headline_image: Path | None = None,
        body_assets: list[Path] | None = None,
        destination_identities: dict[str, str] | None = None,
        require_quality: bool = False,
        legacy_exact8: bool = False,
    ) -> dict[str, Any]:
        if not run_id or not topic_id or safety_status not in {"ALLOW", "BLOCK", "PENDING"}:
            raise InvariantError("run_id, topic_id, and valid safety status are required")
        if max_resume_attempts < 0:
            raise InvariantError("max resume attempts must be non-negative")
        x_post_path = Path(x_post) if x_post is not None else None
        if legacy_exact8 and x_post_path is None:
            x_post_path = Path(run_dir) / "x-post-ja.txt"
        if headline_image is None or not body_assets:
            raise InvariantError("headline image and body media are required")
        headline_image = Path(headline_image)
        body_assets = [Path(path) for path in body_assets]
        destination_identities = (
            dict(destination_identities)
            if destination_identities is not None
            else configured_destination_identities()
        )
        validate_destination_identities(destination_identities)
        self_owned_base_url = _configured_self_owned_base_url()
        resolved_run = self._validate_layout(
            run_id,
            run_dir,
            drafts,
            x_post_path if legacy_exact8 else None,
            headline_image,
            body_assets,
            require_state=False,
        )
        if legacy_exact8 and x_post_path is None:
            raise InvariantError("legacy exact8 requires an x post artifact")
        if x_post_path is not None:
            validate_x_post_text(x_post_path)
        quality_receipts = (
            require_quality_terminals(resolved_run, drafts)
            if require_quality
            else None
        )
        cta_artifacts = {
            "ja": Path(drafts["ja"]),
            "en": Path(drafts["en"]),
        }
        if x_post_path is not None:
            cta_artifacts["x-post-ja"] = x_post_path
        _require_cta(resolved_run, cta_artifacts)
        publication_contract = "legacy-exact8" if legacy_exact8 else "active-four"
        dormant_pairs: dict[str, dict[str, Any]] = {}
        if not legacy_exact8:
            recorded_at = utc_now()
            for dormant_pair in DORMANT_PAIRS:
                dormant_pairs[dormant_pair] = {
                    "platform": dormant_pair.split("/", 1)[0],
                    "lang": dormant_pair.split("/", 1)[1],
                    "status": "skipped",
                    "skip_receipt": {
                        "type": "dormant-destination",
                        "pair": dormant_pair,
                        "reason": "dormant-destination",
                        "slo": "not-applicable",
                        "recorded_at": recorded_at,
                    },
                }
        payload = {
            "version": 1,
            "publication_contract": publication_contract,
            "run_id": run_id,
            "run_dir": str(resolved_run),
            "state_path": str(self.state_path.resolve(strict=False)),
            "ledger_path": str(self.ledger_path.resolve(strict=False)),
            "topic_id": topic_id,
            "destination_identities": destination_identities,
            "self_owned_base_url": self_owned_base_url,
            "safety_status": safety_status,
            "drafts": {
                lang: {"path": str(Path(path).resolve()), "sha256": sha256(Path(path))}
                for lang, path in drafts.items()
            },
            "x_post": (
                {
                    "path": str(x_post_path.resolve()),
                    "sha256": sha256(x_post_path),
                }
                if x_post_path is not None and legacy_exact8
                else {"path": None, "sha256": None}
            ),
            "media": {
                "headline_image": {
                    "path": str(headline_image.resolve()),
                    **descriptor_from_file(headline_image),
                },
                "body_assets": [
                    {"path": str(path.resolve()), **descriptor_from_file(path)}
                    for path in body_assets
                ],
            },
            "pairs": dormant_pairs,
            "resume_attempts": 0,
            "max_resume_attempts": max_resume_attempts,
            "created_at": utc_now(),
        }
        if quality_receipts is not None:
            payload["quality_receipts"] = quality_receipts
            if any(
                receipt.get(key) == "ADVISORY"
                for receipt in quality_receipts.values()
                for key in ("editorial_gate", "reader_gate")
            ):
                payload["quality_force_publish_after_iterations"] = 1
        with self._lock():
            if self.state_path.exists():
                current = self._read_locked()
                current = self._upgrade_legacy_self_owned_base_locked(
                    current, self_owned_base_url
                )
                immutable = (
                    "run_id", "run_dir", "state_path", "ledger_path", "topic_id",
                    "destination_identities", "self_owned_base_url", "drafts", "x_post", "media"
                )
                if require_quality:
                    immutable = (*immutable, "quality_receipts")
                if any(current.get(key) != payload.get(key) for key in immutable):
                    raise InvariantError("refusing to replace another run/topic/draft state")
                return current
            published_hashes = self._published_artifact_hashes(self._ledger_rows_locked())
            for lang, path in drafts.items():
                digest = sha256(Path(path))
                if (lang, digest) in published_hashes:
                    raise InvariantError(
                        f"refusing duplicate published artifact for lang={lang} sha256={digest}"
                    )
            self._write_locked(payload)
            return payload

    def set_safety(self, status: str) -> None:
        if status not in {"ALLOW", "BLOCK", "PENDING"}:
            raise InvariantError("invalid safety status")
        with self._lock():
            state = self._read_locked()
            if self._current_ledger_status_locked(state) == "complete":
                raise InvariantError("terminal publication state is immutable")
            state["safety_status"] = status
            self._write_locked(state)

    def mark_unavailable(self, pair: str, reason: str) -> dict[str, Any]:
        """Record a destination that cannot even be staged.

        Dais 2026-07-25: publish every day no matter what; only safety blocks.
        A dead destination credential (dev.to returned 401) previously left the
        package one intent short of the required set, so the seven staged
        destinations published nothing. An unavailable pair counts as
        registered, is never eligible, and is refused at every publish
        boundary — while its siblings go live. exact8 stays honestly unmet.
        """
        if pair not in SUPPORTED_PAIRS:
            raise InvariantError(f"unknown pair {pair}")
        if not reason:
            raise InvariantError("unavailable requires a reason")
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            if self._current_ledger_status_locked(state) == "complete":
                raise InvariantError("terminal publication state is immutable")
            current = state["pairs"].get(pair, {})
            if current.get("status") == "live":
                raise InvariantError(f"{pair} is already live")
            entry = {
                **current,
                "platform": pair.split("/", 1)[0],
                "lang": pair.split("/", 1)[1],
                "status": "unavailable",
                "error": reason,
                "unavailable_at": utc_now(),
            }
            state["pairs"][pair] = entry
            self._write_locked(state)
            return entry

    def quarantine_missing_media(
        self,
        pair: str,
        target: str,
        reason: str,
        remote: dict[str, Any],
    ) -> dict[str, Any]:
        """Atomically quarantine an X intent after an exact not-live readback.

        The remote probe runs before this method's lock, so every mutable
        publication invariant is re-read and re-checked under the same lock
        immediately before writing ``unavailable``.  A concurrent receipt or
        target transition therefore wins over the quarantine rather than being
        silently downgraded.
        """
        if pair not in {"x-article/ja", "x-article/en"}:
            raise InvariantError("missing-media quarantine requires an X Article pair")
        if not isinstance(remote, dict) or not target or not reason:
            raise InvariantError("missing-media quarantine requires target, reason and proof")
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            if self._current_ledger_status_locked(state) == "complete":
                raise InvariantError("terminal publication state is immutable")
            entry = state.get("pairs", {}).get(pair)
            if (
                not isinstance(entry, dict)
                or entry.get("status") != "intent"
                or entry.get("target_kind") != "x-draft-url"
                or entry.get("target") != target
                or entry.get("receipt")
                or isinstance(entry.get("existing_publication"), dict)
            ):
                raise InvariantError(
                    "missing-media quarantine state changed before the atomic write"
                )
            expected_identity = str(
                state.get("destination_identities", {}).get(pair, "")
            ).strip().lstrip("@")
            if not (
                expected_identity
                and remote.get("status") == "not-live"
                and remote.get("verified") is True
                and remote.get("destination_identity") == expected_identity
                and remote.get("identity_verified") is True
                and remote.get("identity_source") == "x-authenticated-edit-url"
                and remote.get("source") == "x-cdp-saved-article-editor"
            ):
                raise InvariantError(
                    "missing-media quarantine requires an authenticated exact-editor "
                    "not-live proof"
                )
            try:
                receipts = self._current_ledger_receipts_locked(state)
            except InvariantError as error:
                raise InvariantError(
                    "missing-media quarantine refuses an ambiguous publication ledger"
                ) from error
            if pair in receipts:
                raise InvariantError(
                    "missing-media quarantine refuses an existing ledger receipt"
                )
            language = pair.rsplit("/", 1)[1]
            journal_path = (
                Path(str(state.get("run_dir", "")))
                / "gates"
                / "x-inplace-repair"
                / language
                / "journal.json"
            )
            if journal_path.exists():
                try:
                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise InvariantError(
                        "missing-media quarantine refuses an unreadable X repair journal"
                    ) from error
                if not isinstance(journal, dict):
                    raise InvariantError(
                        "missing-media quarantine refuses a malformed X repair journal"
                    )
                if (
                    journal.get("browser_evidence")
                    or journal.get("unpublish_evidence")
                    or journal.get("phase") not in {None, "authorized"}
                ):
                    raise InvariantError(
                        "missing-media quarantine refuses prior X browser-effect evidence"
                    )
            proof_fields = {
                key: remote.get(key)
                for key in (
                    "status",
                    "verified",
                    "destination_identity",
                    "identity_verified",
                    "identity_source",
                    "source",
                )
            }
            entry = {
                **entry,
                "platform": "x-article",
                "lang": language,
                "status": "unavailable",
                "error": (
                    f"{reason}; authenticated exact-editor not-live proof="
                    f"{json.dumps(proof_fields, sort_keys=True, separators=(',', ':'))}"
                ),
                "unavailable_at": utc_now(),
            }
            state["pairs"][pair] = entry
            self._write_locked(state)
            return entry

    def quarantine_identity_conflict(
        self, pair: str, target: str, remote: dict[str, Any]
    ) -> dict[str, Any]:
        """Quarantine a frozen Substack identity conflict without touching the network.

        This is the migration boundary for runs created before the distinct JA/EN
        identity gate. It is idempotent, but requires an authenticated native
        ``not-live`` readback for the exact frozen draft target. A later run must
        be initialized with a genuinely distinct EN publication.
        """

        if pair != "substack/en" or not isinstance(remote, dict) or not target:
            raise InvariantError(
                "identity-conflict quarantine requires substack/en and a remote proof"
            )
        with self._lock():
            state = self._read_locked()
            identities = state.get("destination_identities")
            if not isinstance(identities, dict):
                raise InvariantError("destination identities must be an object")
            japanese = str(identities.get("substack/ja", "")).strip().lower()
            english = str(identities.get("substack/en", "")).strip().lower()
            if not japanese or japanese != english:
                raise InvariantError(
                    "Substack identities are not a conflated legacy pair"
                )
            entry = state.get("pairs", {}).get(pair)
            if not isinstance(entry, dict):
                raise InvariantError("no persisted intent for substack/en")
            if entry.get("status") == "unavailable":
                quarantine = state.get("identity_quarantine")
                if (
                    isinstance(quarantine, dict)
                    and quarantine.get("version") == 1
                    and quarantine.get("pair") == pair
                    and quarantine.get("reason") == IDENTITY_CONFLICT_REASON
                    and quarantine.get("previous_identity") == english
                    and entry.get("error") == IDENTITY_CONFLICT_REASON
                    and not entry.get("receipt")
                ):
                    return dict(entry)
                raise InvariantError("substack/en has a conflicting unavailable state")
            if entry.get("status") != "intent":
                raise InvariantError(
                    "substack/en must be an unambiguous unpublished intent before quarantine"
                )
            if entry.get("receipt"):
                raise InvariantError("substack/en already has a publication receipt")
            validate_target(
                pair,
                str(entry.get("target_kind", "")),
                str(entry.get("target", "")),
            )
            if entry.get("target") != target:
                raise InvariantError("substack/en target changed during remote probe")
            if not (
                remote.get("status") == "not-live"
                and remote.get("verified") is True
                and remote.get("destination_identity") == english
                and remote.get("identity_verified") is True
                and remote.get("identity_source")
                == "protected-substack-authenticated-draft-api"
                and remote.get("source") == "substack-draft-api"
            ):
                raise InvariantError(
                    "identity-conflict quarantine requires authenticated exact-target not-live proof"
                )
            if (
                not english.endswith(".substack.com")
                or "/" in english
            ):
                raise InvariantError("invalid conflated Substack publication identity")
            run_dir = Path(str(state.get("run_dir", "")))
            expected_state = run_dir / "gates" / "publication-state.json"
            expected_ledger = run_dir.parent.parent / "articles.jsonl"
            if (
                self.state_path.is_symlink()
                or self.state_path.resolve(strict=False) != expected_state.resolve(strict=False)
                or self.ledger_path.resolve(strict=False) != expected_ledger.resolve(strict=False)
                or state.get("state_path") != str(self.state_path.resolve(strict=False))
                or state.get("ledger_path") != str(self.ledger_path.resolve(strict=False))
            ):
                raise InvariantError("publication state or ledger is outside the canonical run boundary")
            # Do not invoke the normal identity validator here: this method is
            # precisely the one bounded transition that repairs its legacy
            # conflation.  The canonical run/media/state layout is still checked.
            self._validate_layout(
                str(state.get("run_id", "")),
                Path(str(state.get("run_dir", ""))),
                {
                    lang: Path(str(state.get("drafts", {}).get(lang, {}).get("path", "")))
                    for lang in ("ja", "en")
                },
                Path(str(state.get("x_post", {}).get("path")))
                if self._is_legacy_state(state) and state.get("x_post", {}).get("path")
                else None,
                Path(str(state.get("media", {}).get("headline_image", {}).get("path", ""))),
                [
                    Path(str(item.get("path", "")))
                    for item in state.get("media", {}).get("body_assets", [])
                    if isinstance(item, dict)
                ],
                require_state=True,
            )
            for row in self._ledger_rows_locked():
                if row.get("run_id") == state.get("run_id") and row.get("platform") == "substack" and row.get("lang") == "en":
                    if not _is_no_effect_ledger_row(row, state):
                        raise InvariantError(
                            "substack/en has an effect-capable current-run ledger row; quarantine refused"
                        )
            recorded_at = utc_now()
            entry.update(
                {
                    "status": "unavailable",
                    "error": IDENTITY_CONFLICT_REASON,
                    "unavailable_at": recorded_at,
                }
            )
            state["identity_quarantine"] = {
                "version": 1,
                "pair": pair,
                "reason": IDENTITY_CONFLICT_REASON,
                "previous_identity": english,
                "recorded_at": recorded_at,
            }
            self._write_locked(state)
            return dict(entry)

    def terminalize_invalid_x_post_length(self) -> dict[str, Any]:
        """Quarantine a legacy frozen overlength intent without editing its bytes.

        New runs cannot reach publication init with an invalid X post. This
        transition exists only for states frozen before that gate was added:
        it preserves the original artifact hash, records the measured failure,
        and removes the pair from all future worker eligibility.
        """
        pair = "x-post/ja"
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            if self._current_ledger_status_locked(state) == "complete":
                raise InvariantError("terminal publication state is immutable")
            entry = state.get("pairs", {}).get(pair)
            if not isinstance(entry, dict):
                raise InvariantError("no persisted intent for x-post/ja")
            if entry.get("status") == "live" or entry.get("receipt"):
                raise InvariantError("x-post/ja is already live")
            path = Path(str(state.get("x_post", {}).get("path", "")))
            if (
                path.is_symlink()
                or not path.is_file()
                or sha256(path) != state.get("x_post", {}).get("sha256")
            ):
                raise InvariantError("x-post/ja frozen artifact is missing or changed")
            try:
                measured = len(path.read_text(encoding="utf-8").strip())
            except (OSError, UnicodeError) as error:
                raise InvariantError(f"x post is unreadable: {error}") from error
            if measured <= X_POST_MAX_CHARS:
                raise InvariantError("x-post/ja is not over the pre-freeze length limit")
            if entry.get("status") == "terminal-invalid":
                if (
                    entry.get("terminal_reason") != "invalid_pre_freeze_length"
                    or entry.get("measured_chars") != measured
                    or entry.get("limit_chars") != X_POST_MAX_CHARS
                ):
                    raise InvariantError("conflicting terminal-invalid x-post state")
                return dict(entry)
            if entry.get("status") not in {"intent", "ambiguous"}:
                raise InvariantError(
                    f"x-post/ja cannot become terminal-invalid from {entry.get('status')}"
                )
            entry.update(
                {
                    "status": "terminal-invalid",
                    "terminal_reason": "invalid_pre_freeze_length",
                    "measured_chars": measured,
                    "limit_chars": X_POST_MAX_CHARS,
                    "terminalized_at": utc_now(),
                }
            )
            entry.pop("error", None)
            self._write_locked(state)
            return dict(entry)

    def clear_unavailable(self, pair: str) -> dict[str, Any]:
        """Return a recovered destination to the normal staging path.

        dev.to's key started answering again on 2026-07-26. Removing the
        unavailable entry lets the worker's initialization plan re-create the
        intent from scratch instead of anyone hand-editing state. A live or
        receipted destination is never clearable.
        """
        if pair not in SUPPORTED_PAIRS:
            raise InvariantError(f"unknown pair {pair}")
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            entry = state.get("pairs", {}).get(pair)
            if not entry:
                return {"cleared": False, "reason": "not-registered"}
            if entry.get("status") != "unavailable" or entry.get("receipt"):
                raise InvariantError(
                    f"{pair} is not an unavailable destination"
                )
            reinitialization_pairs = state.setdefault(
                "reinitialization_pairs", []
            )
            if (
                not isinstance(reinitialization_pairs, list)
                or any(item not in SUPPORTED_PAIRS for item in reinitialization_pairs)
            ):
                raise InvariantError("invalid destination reinitialization marker")
            if pair not in reinitialization_pairs:
                reinitialization_pairs.append(pair)
            state["pairs"].pop(pair, None)
            self._write_locked(state)
            return {"cleared": True, "pair": pair}

    def migrate_quarantined_substack_en_identity(
        self, new_identity: str
    ) -> dict[str, Any]:
        """Resolve one legacy JA/EN identity conflation before reinitialization.

        This is the Coconala-style identity rebind boundary: it is allowed only
        for the explicitly quarantined, unpublished EN pair, with no effect-
        capable current-run ledger row. It never changes a live receipt or a
        persisted target; the next initialization tick must create a new EN
        target under the configured authenticated publication.
        """
        identity = str(new_identity).strip().lower()
        if not re.fullmatch(r"[a-z0-9-]+\.substack\.com", identity):
            raise InvariantError("invalid new Substack EN publication identity")
        with self._lock():
            state = self._read_locked()
            identities = state.get("destination_identities")
            if not isinstance(identities, dict):
                raise InvariantError("destination identities must be an object")
            japanese = str(identities.get("substack/ja", "")).strip().lower()
            english = str(identities.get("substack/en", "")).strip().lower()
            if not japanese or japanese != english:
                raise InvariantError("Substack identities are not a conflated legacy pair")
            if identity == japanese:
                raise InvariantError("new English Substack identity must differ from Japanese")
            configured = configured_destination_identities().get("substack/en")
            if configured != identity:
                raise InvariantError(
                    "new English Substack identity must match SUBSTACK_PUBLICATION_EN"
                )
            quarantine = state.get("identity_quarantine")
            if not (
                isinstance(quarantine, dict)
                and quarantine.get("version") == 1
                and quarantine.get("pair") == "substack/en"
                and quarantine.get("reason") == IDENTITY_CONFLICT_REASON
                and quarantine.get("previous_identity") == english
            ):
                raise InvariantError("Substack EN identity conflict is not quarantined")
            reinitialization_pairs = state.get("reinitialization_pairs")
            if not isinstance(reinitialization_pairs, list) or "substack/en" not in reinitialization_pairs:
                raise InvariantError("substack/en is not awaiting identity reinitialization")
            entry = state.get("pairs", {}).get("substack/en")
            if entry is not None and (
                not isinstance(entry, dict)
                or entry.get("receipt")
                or entry.get("status") != "unavailable"
                or entry.get("error") != IDENTITY_CONFLICT_REASON
            ):
                raise InvariantError("Substack EN has a target or effect-capable state")
            run_dir = Path(str(state.get("run_dir", "")))
            expected_state = run_dir / "gates" / "publication-state.json"
            expected_ledger = run_dir.parent.parent / "articles.jsonl"
            if (
                self.state_path.is_symlink()
                or self.state_path.resolve(strict=False) != expected_state.resolve(strict=False)
                or self.ledger_path.resolve(strict=False) != expected_ledger.resolve(strict=False)
                or state.get("state_path") != str(self.state_path.resolve(strict=False))
                or state.get("ledger_path") != str(self.ledger_path.resolve(strict=False))
            ):
                raise InvariantError("publication state or ledger is outside the canonical run boundary")
            self._validate_layout(
                str(state.get("run_id", "")),
                run_dir,
                {
                    lang: Path(str(state.get("drafts", {}).get(lang, {}).get("path", "")))
                    for lang in ("ja", "en")
                },
                None,
                Path(str(state.get("media", {}).get("headline_image", {}).get("path", ""))),
                [
                    Path(str(item.get("path", "")))
                    for item in state.get("media", {}).get("body_assets", [])
                    if isinstance(item, dict)
                ],
                require_state=True,
            )
            for row in self._ledger_rows_locked():
                if (
                    row.get("run_id") == state.get("run_id")
                    and row.get("platform") == "substack"
                    and row.get("lang") == "en"
                    and not _is_no_effect_ledger_row(row, state)
                ):
                    raise InvariantError("substack/en identity migration found an effect-capable ledger row")
            recorded_at = utc_now()
            identities["substack/en"] = identity
            state["identity_migration"] = {
                "version": 1,
                "pair": "substack/en",
                "previous_identity": english,
                "new_identity": identity,
                "reason": "configured-substack-en-identity",
                "recorded_at": recorded_at,
            }
            if isinstance(entry, dict):
                state["pairs"].pop("substack/en", None)
            self._write_locked(state)
            return dict(state["identity_migration"])

    def recover_stale_quality_receipt(self, pair: str) -> dict[str, Any]:
        """Reopen only an obsolete advisory-receipt rejection.

        The old reason is recoverable only after the immutable boundary,
        safety decision, and hash-bound identity/safety receipts validate.
        It never changes draft bytes or quality evidence.
        """
        if pair not in {"devto/en", "substack/ja", "substack/en"}:
            raise InvariantError(f"{pair} cannot recover a stale quality receipt")
        with self._lock():
            state = self._read_locked()
            self._validate_state_boundary_locked(
                state, Path(str(state.get("run_dir", "")))
            )
            if state.get("safety_status") != "ALLOW":
                raise InvariantError("stale quality recovery blocked by safety")
            if not self._drafts_intact(state):
                raise InvariantError("stale quality recovery blocked by changed draft")
            if not self._quality_receipts_intact(state):
                raise InvariantError("stale quality recovery requires current quality receipts")
            entry = state.get("pairs", {}).get(pair)
            if (
                not isinstance(entry, dict)
                or entry.get("status") != "unavailable"
                or entry.get("error") != "publication-intent-stale-quality-receipt"
                or entry.get("receipt")
            ):
                raise InvariantError(f"{pair} is not a recoverable stale quality rejection")
            reinitialization_pairs = state.setdefault("reinitialization_pairs", [])
            if (
                not isinstance(reinitialization_pairs, list)
                or any(item not in SUPPORTED_PAIRS for item in reinitialization_pairs)
            ):
                raise InvariantError("invalid destination reinitialization marker")
            if pair not in reinitialization_pairs:
                reinitialization_pairs.append(pair)
            state["pairs"].pop(pair)
            self._write_locked(state)
            return {"recovered": True, "pair": pair}

    def register_dormant_skip(
        self, pair: str, reason: str = "dormant-destination"
    ) -> dict[str, Any]:
        """Persist a non-publication receipt for a configured dormant pair."""
        if pair not in DORMANT_PAIRS:
            raise InvariantError(f"{pair} is not a dormant destination")
        if not reason:
            raise InvariantError("dormant skip requires a reason")
        with self._lock():
            state = self._read_locked()
            if self._is_legacy_state(state):
                raise InvariantError(
                    "legacy exact8 state does not accept dormant skip receipts"
                )
            self._assert_pair_mutation_allowed(
                state, pair, allow_dormant_skip=True
            )
            if state.get("safety_status") != "ALLOW" or not self._drafts_intact(state):
                raise InvariantError(
                    "publication intent blocked by safety or changed draft"
                )
            if not self._quality_receipts_intact(state):
                raise InvariantError("publication intent blocked by stale quality receipt")
            if self._current_ledger_status_locked(state) == "complete":
                raise InvariantError("terminal publication state is immutable")
            current = state.get("pairs", {}).get(pair)
            if current and current.get("status") not in {"skipped"}:
                raise InvariantError(f"cannot skip an existing intent for {pair}")
            recorded_at = (
                current.get("skip_receipt", {}).get("recorded_at")
                if isinstance(current, dict)
                else None
            ) or utc_now()
            entry = {
                "platform": pair.split("/", 1)[0],
                "lang": pair.split("/", 1)[1],
                "status": "skipped",
                "skip_receipt": {
                    "type": "dormant-destination",
                    "pair": pair,
                    "reason": reason,
                    "slo": "not-applicable",
                    "recorded_at": recorded_at,
                },
            }
            state.setdefault("pairs", {})[pair] = entry
            self._write_locked(state)
            return dict(entry)

    def register_intent(self, pair: str, target_kind: str, target: str) -> dict[str, Any]:
        validate_target(pair, target_kind, target)
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            if state.get("safety_status") != "ALLOW" or not self._drafts_intact(state):
                raise InvariantError(
                    "publication intent blocked by safety or changed draft"
                )
            if not self._quality_receipts_intact(state):
                raise InvariantError("publication intent blocked by stale quality receipt")
            if self._current_ledger_status_locked(state) == "complete":
                raise InvariantError("terminal publication state is immutable")
            current = state["pairs"].get(pair)
            if current and current.get("status") == "unavailable":
                # Re-arm once the destination's credential is fixed.
                current.update(
                    {
                        "target_kind": target_kind,
                        "target": target,
                        "status": "intent",
                        "intent_at": utc_now(),
                    }
                )
                current.pop("error", None)
                current.pop("unavailable_at", None)
                self._write_locked(state)
                return current
            if current:
                if current.get("target_kind") != target_kind or current.get("target") != target:
                    current["status"] = "ambiguous"
                    current["error"] = "conflicting-stable-target"
                    self._write_locked(state)
                    raise InvariantError(f"conflicting target for {pair}")
                if current.get("status") == "ambiguous":
                    current["status"] = "intent"
                    current.pop("error", None)
                    self._write_locked(state)
                return current
            entry = {
                "platform": pair.split("/", 1)[0],
                "lang": pair.split("/", 1)[1],
                "target_kind": target_kind,
                "target": target,
                "status": "intent",
                "intent_at": utc_now(),
            }
            state["pairs"][pair] = entry
            reinitialization_pairs = state.get("reinitialization_pairs", [])
            if isinstance(reinitialization_pairs, list) and pair in reinitialization_pairs:
                reinitialization_pairs.remove(pair)
                if not reinitialization_pairs:
                    state.pop("reinitialization_pairs", None)
            self._write_locked(state)
            return entry

    def register_existing_live(
        self,
        pair: str,
        target_kind: str,
        target: str,
        *,
        live_url: str,
        public_id: str,
        published_at: str,
    ) -> dict[str, Any]:
        """Import a proven legacy live identity without calling it exact8-complete.

        The imported publication remains pending until the current immutable
        media package is installed on that exact remote ID and a fresh strong
        destination readback is recorded.
        """
        validate_target(pair, target_kind, target)
        if not _valid_pair_host(pair, live_url):
            raise InvariantError("existing live URL is invalid for destination")
        if not isinstance(public_id, str) or not public_id.strip():
            raise InvariantError("existing public ID is empty")
        try:
            published = datetime.fromisoformat(
                published_at.replace("Z", "+00:00")
            )
        except (AttributeError, ValueError) as error:
            raise InvariantError(
                "existing published_at is not ISO8601"
            ) from error
        if published.tzinfo is None or published.utcoffset() is None:
            raise InvariantError("existing published_at has no timezone")
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            if self._current_ledger_status_locked(state) == "complete":
                raise InvariantError("terminal publication state is immutable")
            current = state.get("pairs", {}).get(pair)
            if current is None:
                current = {
                    "platform": pair.split("/", 1)[0],
                    "lang": pair.split("/", 1)[1],
                    "target_kind": target_kind,
                    "target": target,
                    "intent_at": utc_now(),
                }
                state["pairs"][pair] = current
            if (
                current.get("target_kind") != target_kind
                or current.get("target") != target
            ):
                raise InvariantError(
                    f"existing live target conflicts with persisted intent for {pair}"
                )
            protected = {
                "live_url": live_url,
                "public_id": public_id,
                "published_at": published_at,
            }
            prior = current.get("existing_publication")
            if prior is not None and prior != protected:
                raise InvariantError(
                    f"existing live identity conflicts with prior import for {pair}"
                )
            if current.get("receipt"):
                raise InvariantError(
                    f"cannot downgrade a verified exact8 receipt for {pair}"
                )
            current["status"] = "repair-required"
            current["existing_publication"] = protected
            current.pop("error", None)
            self._write_locked(state)
            return dict(current)

    def correct_protected_target(
        self,
        pair: str,
        target_kind: str,
        new_target: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        """Correct a donor-derived X edit ID without rebinding its public ID."""
        validate_target(pair, target_kind, new_target)
        if pair != "x-article/ja" or target_kind != "x-draft-url":
            raise InvariantError(
                "protected target correction is limited to imported X JA"
            )
        if not isinstance(evidence, dict):
            raise InvariantError("protected target correction evidence is missing")
        with self._lock():
            state = self._read_locked()
            if self._current_ledger_status_locked(state) == "complete":
                raise InvariantError("terminal publication state is immutable")
            entry = state.get("pairs", {}).get(pair, {})
            protected = entry.get("existing_publication", {})
            old_target = str(entry.get("target", ""))
            public_id = str(protected.get("public_id", ""))
            live_url = str(protected.get("live_url", ""))
            expected_identity = state.get(
                "destination_identities", {}
            ).get(pair)
            expected_evidence = {
                "source": "x-authenticated-published-dashboard",
                "destination_identity": expected_identity,
                "identity_verified": True,
                "public_id": public_id,
                "live_url": live_url,
                "dashboard_view_url": (
                    f"https://x.com/{expected_identity}/status/{public_id}"
                ),
                "old_target": old_target,
                "new_target": new_target,
            }
            prior = entry.get("target_correction")
            if (
                old_target == new_target
                and isinstance(prior, dict)
                and prior.get("evidence") == evidence
            ):
                return dict(entry)
            old_match = re.fullmatch(
                r"https://x\.com/compose/articles/edit/([0-9]{8,})",
                old_target,
            )
            new_match = re.fullmatch(
                r"https://x\.com/compose/articles/edit/([0-9]{8,})",
                new_target,
            )
            if (
                entry.get("status") != "repair-required"
                or entry.get("target_kind") != target_kind
                or entry.get("receipt")
                or not public_id
                or old_match is None
                or old_match.group(1) != public_id
                or new_match is None
                or new_target == old_target
                or evidence != expected_evidence
            ):
                raise InvariantError(
                    "protected X edit target correction evidence conflicts"
                )
            if any(
                row.get("run_id") == state.get("run_id")
                and row.get("topic_id") == state.get("topic_id")
                and row.get("platform") == "x-article"
                and row.get("lang") == "ja"
                for row in self._ledger_rows_locked()
            ):
                raise InvariantError(
                    "cannot correct a target after its live ledger receipt"
                )
            entry["target"] = new_target
            entry["target_correction"] = {
                "corrected_at": utc_now(),
                "evidence": evidence,
            }
            self._write_locked(state)
            return dict(entry)

    # Each recoverable ambiguity binds one pair to its exact persisted errors
    # and one exact authenticated remote evidence shape. Anything else refuses.
    AMBIGUITY_RECOVERY_RULES: dict[str, dict[str, Any]] = {
        "note/ja": {
            # A paid note exposes only its free teaser anonymously. Older
            # readback therefore froze a successful publish as canonical
            # content mismatch; the current probe authenticates as the owner
            # and proves the complete paid body before recovery.
            "errors": (
                "canonical-content-readback-failed",
                "public-asset-readback-failed",
            ),
            "identity_source": "note-public-canonical-url",
            "source": "note-api+anonymous-public-html",
            "mismatch_to_repair": True,
        },
        # Two authenticated shapes prove an X article is still a draft: the
        # saved-editor view, and the editor URL redirecting to a status page
        # that renders X's not-found empty state (measured live 2026-07-26).
        # canonical-content-readback-failed additionally covers an X Article
        # that is genuinely public (title + text already match the immutable
        # draft) but whose table-image journal a later in-place repair
        # records does not exist yet (measured live 2026-07-26:
        # daily-2026-07-26 x-article/ja froze in exactly this circle --
        # publication_remote.x_table_evidence_gap proves that narrower shape
        # as "live-media-mismatch", never a plain not-live/live verdict).
        # The recovered public ID is never equal to the persisted edit-URL
        # target for X (unlike Substack's same-ID draft), so this pair skips
        # that equality check; every other authenticated-shape check still
        # applies unchanged.
        "x-article/en": {
            "errors": (
                "ambiguous-x-draft-view",
                "x-article-body-scope-ambiguous",
                "canonical-content-readback-failed",
                "public-asset-readback-failed",
                "destination-media-readback-failed",
            ),
            "error_prefixes": (
                "remote-probe-error:URLError:",
            ),
            "evidence_shapes": (
                ("x-authenticated-edit-url", "x-cdp-saved-article-editor"),
                (
                    "x-authenticated-account-navbar",
                    "x-authenticated-missing-status-page",
                ),
            ),
            "identity_source": "x-public-canonical-account-path",
            "source": "x-authenticated-cdp-public-article",
            "mismatch_to_repair": True,
            "skip_public_id_target_check": True,
        },
        "x-article/ja": {
            "errors": (
                "ambiguous-x-draft-view",
                "x-article-body-scope-ambiguous",
                "canonical-content-readback-failed",
                "public-asset-readback-failed",
                "destination-media-readback-failed",
            ),
            "error_prefixes": (
                "remote-probe-error:URLError:",
            ),
            "evidence_shapes": (
                ("x-authenticated-edit-url", "x-cdp-saved-article-editor"),
                (
                    "x-authenticated-account-navbar",
                    "x-authenticated-missing-status-page",
                ),
            ),
            "identity_source": "x-public-canonical-account-path",
            "source": "x-authenticated-cdp-public-article",
            "mismatch_to_repair": True,
            "skip_public_id_target_check": True,
        },
        "x-post/ja": {
            # The daily post slot freezes when a publish attempt cannot be
            # classified; an authenticated timeline read that shows no such
            # post is a decisive not-live verdict (2026-07-26).
            "errors": (
                "remote-state-unknown",
                "ambiguous-x-post-readback",
                "x-post-effect-uncertain-awaiting-timeline-readback",
                "x-post-editor-unavailable-bounded-retry",
            ),
            "evidence_shapes": (
                (
                    "x-authenticated-account-timeline",
                    "x-authenticated-account-timeline",
                ),
            ),
        },
        "devto/en": {
            "errors": (
                "missing-devto-api-key",
                "devto-target-missing-from-owned-drafts",
                "public-asset-readback-failed",
                "destination-media-readback-failed",
            ),
            "identity_source": "devto-authenticated-article-api",
            "source": "devto-authenticated-api+anonymous-public-html",
            "mismatch_to_repair": True,
            "evidence_shapes": (
                (
                    "devto-authenticated-unpublished-api",
                    "devto-authenticated-unpublished-api",
                ),
                (
                    "devto-authenticated-article-api",
                    "devto-authenticated-article-api",
                ),
            ),
        },
        "substack/ja": {
            "errors": ("public-asset-readback-failed",),
            "error_prefixes": (
                "remote-probe-error:URLError:<urlopen error [Errno 8] nodename nor servname provided",
            ),
            "identity_source": "protected-substack-publication-host",
            "source": "substack-draft-api+anonymous-public-html",
            "mismatch_to_repair": True,
        },
        "substack/en": {
            "errors": ("public-asset-readback-failed",),
            "error_prefixes": (
                "remote-probe-error:URLError:<urlopen error [Errno 8] nodename nor servname provided",
            ),
            "identity_source": "protected-substack-publication-host",
            "source": "substack-draft-api+anonymous-public-html",
            "mismatch_to_repair": True,
        },
    }

    def recover_ambiguous_intent(
        self,
        pair: str,
        remote: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve one recoverable ambiguity only from its authenticated destination."""
        rule = self.AMBIGUITY_RECOVERY_RULES.get(pair)
        if rule is None or not isinstance(remote, dict):
            raise InvariantError(
                "pair has no bounded ambiguity recovery rule"
            )
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            entry = state.get("pairs", {}).get(pair, {})
            validate_target(
                pair,
                str(entry.get("target_kind", "")),
                str(entry.get("target", "")),
            )
            if (
                entry.get("status") != "ambiguous"
                or not _rule_error_matches(rule, entry.get("error"))
                or entry.get("receipt")
            ):
                raise InvariantError(
                    "persisted state is not a recoverable ambiguity"
                )
            if (
                pair in {"x-article/ja", "x-article/en"}
                and remote.get("status") == "unknown"
                and remote.get("reason") in rule["errors"]
                and _remote_identity_verified(state, pair, remote)
            ):
                # A bounded same-ID recovery that still returns a recognized
                # ambiguity has no safe publish action. Only an authenticated
                # destination proof may quarantine it; a bare reason string
                # from a wrong browser tab (for example a non-X redirect)
                # must remain ambiguous rather than changing state.
                reason = str(remote["reason"])
                entry["status"] = "unavailable"
                entry["error"] = (
                    f"ambiguity-recovery-unresolved:{reason}"
                )
                entry["unavailable_at"] = utc_now()
                entry["ambiguity_recovery_failure"] = {
                    "observed_at": utc_now(),
                    "evidence": remote,
                }
                self._write_locked(state)
                return dict(entry)
            if (
                rule.get("mismatch_to_repair")
                and remote.get("status")
                in {"live-media-mismatch", "live-content-mismatch"}
            ):
                # The destination proved the protected identity, public ID,
                # and every invariant except one explicitly bounded media or
                # final-CTA gap. Re-label to repair-required so the worker's
                # fixed same-ID dispatch restores the immutable artifact; no
                # remote mutation happens here.
                if (
                    remote.get("verified") is not True
                    or remote.get("destination_identity")
                    != state.get("destination_identities", {}).get(pair)
                    or remote.get("identity_verified") is not True
                    or remote.get("identity_source")
                    != rule["identity_source"]
                    or remote.get("source") != rule["source"]
                    or (
                        not rule.get("skip_public_id_target_check")
                        and str(remote.get("public_id", ""))
                        != str(entry.get("target", ""))
                    )
                    or not valid_http_url(remote.get("live_url"))
                ):
                    raise InvariantError(
                        "authenticated destination does not resolve ambiguity"
                    )
                entry["status"] = "repair-required"
                entry.pop("error", None)
                entry["existing_publication"] = {
                    "live_url": str(remote["live_url"]),
                    "public_id": str(remote["public_id"]),
                }
                entry["ambiguity_recovery"] = {
                    "recovered_at": utc_now(),
                    "evidence": remote,
                }
                self._write_locked(state)
                return dict(entry)
            if (
                remote.get("status") == "live"
                and remote.get("verified") is True
            ):
                # The destination proved the publish actually happened. The full
                # receipt boundary (content, media, identity, hashes) is enforced
                # by validate_receipt_evidence inside the record path; anything
                # weaker refuses and leaves the ambiguity untouched.
                return self._record_live_locked(
                    state,
                    pair,
                    str(remote.get("live_url", "")),
                    remote,
                    reread_remote_assets=not pair.startswith("x-article/"),
                )
            expected_identity = state.get(
                "destination_identities", {}
            ).get(pair)
            if (
                remote.get("status") != "not-live"
                or remote.get("verified") is not True
                or remote.get("destination_identity")
                != expected_identity
                or remote.get("identity_verified") is not True
                or (
                    remote.get("identity_source"),
                    remote.get("source"),
                )
                not in _rule_evidence_shapes(rule)
            ):
                raise InvariantError(
                    "authenticated destination does not resolve ambiguity"
                )
            entry["status"] = "intent"
            entry.pop("error", None)
            entry["ambiguity_recovery"] = {
                "recovered_at": utc_now(),
                "evidence": remote,
            }
            self._write_locked(state)
            return dict(entry)

    def recover_unavailable_live(
        self,
        pair: str,
        remote: dict[str, Any],
    ) -> dict[str, Any]:
        """Mint a receipt when a post-PUT Dev.to target becomes visible later.

        Dev.to can remove a just-published article from the unpublished list
        before the public article endpoint starts returning it. Older workers
        quarantined that exact propagation gap as unavailable. Recovery is
        deliberately narrower than clearing/re-staging: only the persisted
        numeric ID and exact failure class may become live, and the ordinary
        strong receipt validator still proves content, media, identity, and
        immutable hashes. No publisher call occurs on this path.
        """
        if pair != "devto/en" or not isinstance(remote, dict):
            raise InvariantError(
                "pair has no bounded unavailable-live recovery rule"
            )
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            entry = state.get("pairs", {}).get(pair, {})
            validate_target(
                pair,
                str(entry.get("target_kind", "")),
                str(entry.get("target", "")),
            )
            if (
                entry.get("status") != "unavailable"
                or entry.get("error")
                != "devto-target-missing-from-owned-drafts-after-publish-put"
                or entry.get("receipt")
            ):
                raise InvariantError(
                    "persisted state is not a recoverable unavailable Dev.to publish"
                )
            if (
                remote.get("status") != "live"
                or remote.get("verified") is not True
            ):
                return {
                    "action": "still-unavailable",
                    "pair": pair,
                    "target": entry["target"],
                }
            return self._record_live_locked(
                state,
                pair,
                str(remote.get("live_url", "")),
                remote,
            )

    def _drafts_intact(self, state: dict[str, Any]) -> bool:
        run_dir = Path(str(state.get("run_dir", "")))
        if not run_dir.is_dir() or run_dir.is_symlink():
            return False
        resolved_run = run_dir.resolve()
        if resolved_run.name != state.get("run_id"):
            return False
        drafts = state.get("drafts", {})
        if set(drafts) != {"ja", "en"}:
            return False
        for lang in ("ja", "en"):
            draft = drafts[lang]
            path = Path(str(draft.get("path", "")))
            if (
                path.is_symlink()
                or not path.is_file()
                or path.resolve() != resolved_run / f"article-{lang}.md"
                or sha256(path) != draft.get("sha256")
            ):
                return False
        x_post = state.get("x_post", {})
        x_post_value = x_post.get("path") if isinstance(x_post, dict) else None
        if self._is_legacy_state(state):
            if not x_post_value:
                return False
            x_post_path = Path(str(x_post_value))
            if (
                x_post_path.is_symlink()
                or not x_post_path.is_file()
                or x_post_path.resolve() != resolved_run / "x-post-ja.txt"
                or sha256(x_post_path) != x_post.get("sha256")
            ):
                return False
        media = state.get("media", {})
        headline = media.get("headline_image", {})
        headline_path = Path(str(headline.get("path", "")))
        if (
            headline_path.is_symlink()
            or not headline_path.is_file()
            or headline_path.resolve() != resolved_run / "headline-image.png"
            or sha256(headline_path) != headline.get("sha256")
        ):
            return False
        body_assets = media.get("body_assets", [])
        if not isinstance(body_assets, list) or not body_assets:
            return False
        resolved_assets: set[Path] = set()
        headline_digest = str(headline.get("sha256", ""))
        media_hashes = {headline_digest}
        for asset in body_assets:
            if not isinstance(asset, dict):
                return False
            path = Path(str(asset.get("path", "")))
            if (
                path.is_symlink()
                or not path.is_file()
                or path.resolve().parent != resolved_run
                or path.resolve() == headline_path.resolve()
                or path.resolve() in resolved_assets
                or sha256(path) != asset.get("sha256")
                or str(asset.get("sha256", "")) in media_hashes
            ):
                return False
            media_hashes.add(str(asset.get("sha256", "")))
            resolved_assets.add(path.resolve())
        return True

    @staticmethod
    def _quality_receipts_intact(state: dict[str, Any]) -> bool:
        """Keep persisted quality proof bound to the current frozen hashes."""
        receipts = state.get("quality_receipts")
        if receipts is None:
            # Legacy unarmed fixtures predate quality receipts and remain
            # resumable; armed/current states always carry this object.
            return True
        if not isinstance(receipts, dict):
            return False
        drafts = state.get("drafts", {})
        for lang in ("ja", "en"):
            receipt = receipts.get(lang)
            draft = drafts.get(lang, {}) if isinstance(drafts, dict) else {}
            if not isinstance(receipt, dict) or any(
                receipt.get(key) != expected
                for key, expected in {
                    "status": "terminal",
                    "lang": lang,
                    "article_sha256": draft.get("sha256"),
                    "identity_gate": "PASS",
                    "safety_gate": "ALLOW",
                }.items()
            ):
                return False
            if (
                receipt.get("editorial_gate") not in {"PASS", "ADVISORY"}
                or receipt.get("reader_gate") not in {"PASS", "ADVISORY"}
            ):
                return False
            if (
                receipt.get("editorial_gate") == "ADVISORY"
                or receipt.get("reader_gate") == "ADVISORY"
            ) and state.get("quality_force_publish_after_iterations") != 1:
                return False
        return True

    def _all_intents_valid(
        self, state: dict[str, Any], *, allow_ambiguous: bool = False
    ) -> bool:
        """Require the complete current-run publish set before the first irreversible call.

        With allow_ambiguous, a frozen ambiguous pair does not invalidate the
        set: it stays excluded from eligibility and refused by guard, but one
        stuck destination never holds every other pair hostage (Dais
        2026-07-25 after note/ja waited hours behind an x-article/ja
        ambiguity).
        """
        allowed = {"intent", "repair-required", "live"}
        if allow_ambiguous:
            # A frozen ambiguous pair or an unavailable destination stays
            # registered but never publishes; siblings must not be held
            # hostage by it (Dais 2026-07-25).
            allowed = allowed | {"ambiguous", "unavailable", "terminal-invalid"}
        if not self._pair_set_valid(state) or not self._quality_receipts_intact(state):
            return False
        try:
            for pair in self._required_pairs_for_state(state):
                entry = state["pairs"][pair]
                if entry.get("status") == "unavailable":
                    continue
                validate_target(pair, str(entry.get("target_kind", "")), str(entry.get("target", "")))
                if entry.get("status") not in allowed:
                    return False
                if entry.get("status") == "live" and not valid_http_url(
                    entry.get("receipt", {}).get("live_url")
                ):
                    return False
        except (KeyError, InvariantError):
            return False
        return bool(state.get("run_id")) and bool(state.get("topic_id"))

    def _ledger_rows_locked(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        rows = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    @staticmethod
    def _published_artifact_hashes(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
        return {
            (str(row.get("lang")), str(row.get("artifact_sha256")))
            for row in rows
            if row.get("published") is True
            and row.get("lang") in {"ja", "en"}
            and re.fullmatch(r"[0-9a-f]{64}", str(row.get("artifact_sha256", "")))
        }

    def _assert_no_duplicate_published_artifact_locked(
        self, state: dict[str, Any], pair: str
    ) -> None:
        """Enforce the cross-run article identity fence at every publish boundary.

        Initialization performs the same check for a new state, but an existing
        state must not bypass it simply because a publisher resumed later.  The
        current run may legitimately publish one immutable article to several
        destinations; a different run may not publish that language/hash again.
        """

        artifact_sha256 = _expected_artifact_hash(state, pair)
        if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
            raise InvariantError("immutable publication artifact hash is missing")
        lang = str(state.get("pairs", {}).get(pair, {}).get("lang", ""))
        for row in self._ledger_rows_locked():
            if (
                row.get("published") is True
                and row.get("run_id") != state.get("run_id")
                and row.get("lang") == lang
                and row.get("artifact_sha256") == artifact_sha256
            ):
                raise InvariantError(
                    "refusing duplicate published artifact at publication boundary "
                    f"for lang={lang} sha256={artifact_sha256}"
                )

    def _current_ledger_receipts_locked(self, state: dict[str, Any]) -> dict[str, str]:
        """Return verified pair receipts; reject duplicate or malformed current-run rows."""
        current = [
            row
            for row in self._ledger_rows_locked()
            if row.get("run_id") == state.get("run_id")
            and row.get("topic_id") == state.get("topic_id")
            and row.get("published") is True
            and not is_self_owned_publication_receipt(row, state)
        ]
        receipts: dict[str, str] = {}
        required = set(self._required_pairs_for_state(state))
        historical_alias = self._is_active_alias_state(state)
        for row in current:
            pair = f"{row.get('platform', '')}/{row.get('lang', '')}"
            if pair not in required and historical_alias and pair in {
                "zenn-article/ja",
                "devto/en",
            }:
                continue
            if (
                pair not in required
                or pair in receipts
                or row.get("reality_gate") != "PASS"
                or not valid_http_url(row.get("live_url"))
            ):
                raise InvariantError("ambiguous current-run publication ledger")
            try:
                validate_receipt_evidence(
                    state,
                    pair,
                    str(row["live_url"]),
                    row,
                    reread_remote_assets=False,
                )
            except InvariantError as error:
                raise InvariantError(
                    f"invalid current-run publication receipt for {pair}: {error}"
                ) from error
            live_url = str(row["live_url"])
            state_entry = state.get("pairs", {}).get(pair, {})
            if state_entry.get("status") == "live":
                state_url = state_entry.get("receipt", {}).get("live_url")
                if state_url != live_url:
                    raise InvariantError("state and ledger live URLs conflict")
            receipts[pair] = live_url
        return receipts

    def _current_ledger_status_locked(self, state: dict[str, Any]) -> str:
        """Return incomplete, complete, or ambiguous for this exact run/topic ledger set."""
        try:
            receipts = self._current_ledger_receipts_locked(state)
        except InvariantError:
            return "ambiguous"
        return (
            "complete"
            if set(receipts) == set(self._required_pairs_for_state(state))
            else "incomplete"
        )

    def _repair_ledger_locked(
        self,
        state: dict[str, Any],
        pair: str,
        live_url: str,
        evidence: dict[str, Any],
    ) -> bool:
        entry = state["pairs"][pair]
        current = [
            row for row in self._ledger_rows_locked()
            if row.get("run_id") == state["run_id"]
            and row.get("topic_id") == state["topic_id"]
            and row.get("platform") == entry["platform"]
            and row.get("lang") == entry["lang"]
            and row.get("published") is True
        ]
        if current:
            if len(current) != 1 or current[0].get("live_url") != live_url:
                entry["status"] = "ambiguous"
                entry["error"] = "conflicting-current-run-ledger"
                self._write_locked(state)
                raise InvariantError(f"conflicting current-run ledger for {pair}")
            return False
        row = {
            "ts": utc_now(),
            "run_id": state["run_id"],
            "topic_id": state["topic_id"],
            "topic": state["topic_id"],
            "platform": entry["platform"],
            "lang": entry["lang"],
            "live_url": live_url,
            "state": "live",
            "verified_logged_in": True,
            "published": True,
            "reality_gate": "PASS",
            **{
                key: evidence[key]
                for key in (
                    "verified",
                    "public_id",
                    "published_at",
                    "stable_target",
                    "artifact_sha256",
                    "language",
                    "content_verified",
                    "asset_hashes",
                    "asset_urls",
                    "asset_proofs",
                    "native_asset_count",
                    "asset_verified",
                    "eyecatch_verified",
                    "body_media_verified",
                    "cover_verified",
                    "timeline_verified",
                    "emoji_verified",
                    "status_id",
                    "source",
                    "readback_source",
                    "destination_identity",
                    "identity_verified",
                    "identity_source",
                )
                if key in evidence
            },
        }
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())
        return True

    def _record_live_locked(
        self,
        state: dict[str, Any],
        pair: str,
        live_url: str,
        evidence: dict[str, Any],
        *,
        reread_remote_assets: bool = True,
    ) -> dict[str, Any]:
        self._assert_pair_mutation_allowed(state, pair)
        if self._current_ledger_status_locked(state) == "complete":
            raise InvariantError("terminal publication state is immutable")
        if pair not in state.get("pairs", {}):
            raise InvariantError(f"no persisted intent for {pair}")
        self._assert_no_duplicate_published_artifact_locked(state, pair)
        if state["safety_status"] != "ALLOW" or not self._drafts_intact(state):
            raise InvariantError("safety is not ALLOW or immutable drafts changed")
        if not valid_http_url(live_url) or evidence.get("verified") is not True:
            raise InvariantError("remote verified live URL is required")
        validate_receipt_evidence(
            state,
            pair,
            live_url,
            evidence,
            reread_remote_assets=reread_remote_assets,
        )
        entry = state["pairs"][pair]
        protected = entry.get("existing_publication")
        if isinstance(protected, dict):
            if live_url != protected.get("live_url"):
                raise InvariantError(
                    f"receipt changed protected live URL for {pair}"
                )
            if evidence.get("public_id") != protected.get("public_id"):
                raise InvariantError(
                    f"receipt changed protected live public ID for {pair}"
                )
        existing = entry.get("receipt", {}).get("live_url")
        if existing and existing != live_url:
            entry["status"] = "ambiguous"
            entry["error"] = "conflicting-live-url"
            self._write_locked(state)
            raise InvariantError(f"conflicting live URL for {pair}")
        entry.pop("error", None)
        entry["status"] = "live"
        entry["receipt"] = {"live_url": live_url, "evidence": evidence, "recorded_at": utc_now()}
        repaired = self._repair_ledger_locked(state, pair, live_url, evidence)
        self._write_locked(state)
        self._notify_pair_live(state, pair, live_url)
        return {"action": "skip-live", "live_url": live_url, "repaired": repaired}

    def _notify_pair_live(
        self, state: dict[str, Any], pair: str, live_url: str
    ) -> None:
        """One Telegram message per destination the moment it goes live.

        Dais 2026-07-25: report each link immediately, never one giant
        end-of-run message. Strictly best-effort — a notify failure must
        never break a minted receipt.
        """
        # Fire only inside the armed production loop (wrappers always export
        # ARTICLE_AUTOPUBLISH=1); unit tests and dry contexts stay silent.
        if os.environ.get("ARTICLE_AUTOPUBLISH") != "1":
            return
        target = os.environ.get("ARTICLE_NOTIFY_TARGET", "8547730585")
        if not target:
            return
        labels = {
            "note/ja": "Note（日本語）",
            "substack/ja": "Substack（日本語）",
            "substack/en": "Substack（英語）",
            "x-article/ja": "X記事（日本語）",
            "x-article/en": "X記事（英語）",
            "devto/en": "Dev.to（英語）",
            "zenn-article/ja": "Zenn（日本語）",
            "x-post/ja": "X短文（日本語）",
        }
        label = labels.get(pair, "公開先")
        message = (
            f"{label}の公開後の読み戻しを確認しました。\n"
            f"公開URL：{live_url}\n"
            "残りの公開先は、同じ記事を使って自動的に確認します。"
        )
        try:
            telegram_api_transport(target)(message)
        except Exception:
            pass

    def record_live(self, pair: str, live_url: str, evidence: dict[str, Any]) -> dict[str, Any]:
        with self._lock():
            return self._record_live_locked(self._read_locked(), pair, live_url, evidence)

    def guard(self, pair: str, remote: dict[str, Any]) -> dict[str, Any]:
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            if pair not in state.get("pairs", {}):
                raise InvariantError(f"no persisted intent for {pair}")
            if state["safety_status"] != "ALLOW" or not self._drafts_intact(state):
                raise InvariantError("publication is blocked by safety or changed draft")
            if not self._all_intents_valid(state, allow_ambiguous=True):
                raise InvariantError(self._intent_requirement_message(state))
            self._assert_no_duplicate_published_artifact_locked(state, pair)
            if state["pairs"][pair].get("status") in {
                "ambiguous",
                "unavailable",
                "terminal-invalid",
            }:
                raise InvariantError(
                    f"pair {pair} is frozen ({state['pairs'][pair].get('status')}) "
                    "and requires bounded recovery"
                )
            entry = state["pairs"][pair]
            if entry.get("status") == "repair-required":
                # This path can only mutate the already-protected public ID.
                # An unrelated historical receipt that no longer reproduces
                # under today's media calibration must still block creates,
                # but it cannot make a same-ID repair impossible forever.
                if (
                    remote.get("status") == "live"
                    and remote.get("verified") is True
                ):
                    return self._record_live_locked(
                        state,
                        pair,
                        str(remote.get("live_url", "")),
                        remote,
                        reread_remote_assets=not pair.startswith("x-article/"),
                    )
                protected = entry.get("existing_publication", {})
                return {
                    "action": "repair-live",
                    "pair": pair,
                    "target": entry["target"],
                    "live_url": protected.get("live_url"),
                    "public_id": protected.get("public_id"),
                }
            ledger_status = self._current_ledger_status_locked(state)
            if ledger_status == "ambiguous":
                raise InvariantError("ambiguous current-run publication ledger")
            ledger_receipts = self._current_ledger_receipts_locked(state)
            if pair in ledger_receipts:
                return {
                    "action": "skip-live",
                    "live_url": ledger_receipts[pair],
                    "repaired": False,
                }
            if entry.get("status") == "live":
                live_url = entry.get("receipt", {}).get("live_url")
                if not valid_http_url(live_url):
                    raise InvariantError("live state has no valid receipt URL")
                receipt_evidence = entry.get("receipt", {}).get("evidence", {})
                if not isinstance(receipt_evidence, dict):
                    raise InvariantError("live state has no valid receipt evidence")
                repaired = self._repair_ledger_locked(
                    state, pair, live_url, receipt_evidence
                )
                if repaired:
                    self._write_locked(state)
                return {"action": "skip-live", "live_url": live_url, "repaired": repaired}
            if remote.get("status") == "live" and remote.get("verified") is True:
                return self._record_live_locked(
                    state,
                    pair,
                    str(remote.get("live_url", "")),
                    remote,
                    reread_remote_assets=not pair.startswith("x-article/"),
                )
            if (
                remote.get("status") == "not-live"
                and remote.get("verified") is True
                and _remote_identity_verified(state, pair, remote)
            ):
                return {
                    "action": "publish",
                    "pair": pair,
                    "target": entry["target"],
                    "remote": remote,
                }
            entry["status"] = "ambiguous"
            entry["error"] = str(remote.get("reason") or "remote-state-unknown")
            self._write_locked(state)
            raise InvariantError(f"ambiguous remote state for {pair}; refusing publish")

    def assert_ready(self, pair: str) -> dict[str, Any]:
        """Read-only pre-probe check used by mandatory publisher boundaries."""
        with self._lock():
            state = self._read_locked()
            self._assert_pair_mutation_allowed(state, pair)
            if pair not in state.get("pairs", {}):
                raise InvariantError(f"no persisted intent for {pair}")
            if state.get("safety_status") != "ALLOW" or not self._drafts_intact(state):
                raise InvariantError("publication is blocked by safety or changed draft")
            if not self._all_intents_valid(state, allow_ambiguous=True):
                raise InvariantError(self._intent_requirement_message(state))
            if state["pairs"][pair].get("status") in {
                "ambiguous",
                "unavailable",
                "terminal-invalid",
            }:
                raise InvariantError(
                    f"pair {pair} is frozen ({state['pairs'][pair].get('status')}) "
                    "and requires bounded recovery"
                )
            return dict(state["pairs"][pair])

    def _plan_locked(
        self, state: dict[str, Any], *, enforce_resume_budget: bool = True
    ) -> dict[str, Any]:
        try:
            self._validate_state_boundary_locked(
                state, Path(str(state.get("run_dir", "")))
            )
        except InvariantError:
            return {"resumable": False, "reason": "run-or-draft-boundary-invalid"}
        if state.get("safety_status") == "BLOCK":
            return {"resumable": False, "reason": "safety-block"}
        if state.get("safety_status") != "ALLOW":
            return {"resumable": False, "reason": "safety-not-terminal"}
        if not self._drafts_intact(state):
            return {"resumable": False, "reason": "draft-changed-or-missing"}
        if not self._pair_set_valid(state):
            return {"resumable": False, "reason": "missing-targets"}
        if not self._all_intents_valid(state, allow_ambiguous=True):
            return {"resumable": False, "reason": "ambiguous-target-or-reality"}
        recovery_pairs = [
            pair
            for pair in self._required_pairs_for_state(state)
            if (
                state["pairs"][pair].get("status") == "ambiguous"
                and pair in self.AMBIGUITY_RECOVERY_RULES
                and state["pairs"][pair].get("error")
                in self.AMBIGUITY_RECOVERY_RULES[pair]["errors"]
            )
        ]
        repair_pairs = [
            pair
            for pair in self._required_pairs_for_state(state)
            if state["pairs"][pair].get("status") == "repair-required"
        ]
        ledger_status = self._current_ledger_status_locked(state)
        if ledger_status == "ambiguous":
            if recovery_pairs or repair_pairs:
                return {
                    "resumable": True,
                    "reason": "recovery-only-despite-legacy-ledger-drift",
                    "run_id": state["run_id"],
                    "run_dir": state["run_dir"],
                    "topic_id": state["topic_id"],
                    "drafts": state["drafts"],
                    "pending_pairs": repair_pairs,
                    "recovery_pairs": recovery_pairs,
                    "pairs": {
                        pair: state["pairs"][pair]
                        for pair in self._required_pairs_for_state(state)
                    },
                    "next_attempt": int(state.get("resume_attempts", 0)) + 1,
                    "max_resume_attempts": int(
                        state["max_resume_attempts"]
                    ),
                }
            return {
                "resumable": False,
                "reason": "ambiguous-target-or-reality",
            }
        if ledger_status == "complete":
            return {"resumable": False, "reason": "all-complete"}
        ledger_receipts = self._current_ledger_receipts_locked(state)
        recovery_pairs = [
            pair for pair in recovery_pairs if pair not in ledger_receipts
        ]
        pending = [
            pair
            for pair in self._required_pairs_for_state(state)
            if pair not in ledger_receipts
            # A frozen ambiguous pair waits for bounded recovery; it is never
            # eligible work, and it no longer blocks its siblings.
            and state["pairs"][pair].get("status")
            not in {"ambiguous", "unavailable", "terminal-invalid"}
        ]
        if not pending and not recovery_pairs:
            frozen_unavailable = [
                pair
                for pair in self._required_pairs_for_state(state)
                if state["pairs"][pair].get("status") == "unavailable"
            ]
            if frozen_unavailable:
                # An unavailable destination is deliberately excluded from
                # automatic publication, but it is not completion evidence.
                # Returning all-complete here made an armed run look finished
                # forever after a transient staging failure and suppressed the
                # explicit re-arm path (clear-unavailable/register-intent).
                return {
                    "resumable": False,
                    "reason": "frozen-incomplete-pairs",
                    "frozen_pairs": frozen_unavailable,
                }
            if any(
                entry.get("status") == "terminal-invalid"
                for entry in state["pairs"].values()
            ):
                return {
                    "resumable": False,
                    "reason": "terminal-invalid-pairs",
                }
            return {"resumable": False, "reason": "all-complete"}
        if (
            enforce_resume_budget
            and int(state.get("resume_attempts", 0))
            >= int(state.get("max_resume_attempts", 0))
        ):
            return {"resumable": False, "reason": "resume-budget-exhausted"}
        return {
            "resumable": True,
            "reason": "pending-publication-pairs",
            "run_id": state["run_id"],
            "run_dir": state["run_dir"],
            "topic_id": state["topic_id"],
            "drafts": state["drafts"],
            "pending_pairs": pending,
            "recovery_pairs": recovery_pairs,
            "pairs": {
                pair: state["pairs"][pair]
                for pair in self._required_pairs_for_state(state)
            },
            "next_attempt": int(state.get("resume_attempts", 0)) + 1,
            "max_resume_attempts": int(state["max_resume_attempts"]),
        }

    def plan(self) -> dict[str, Any]:
        with self._lock():
            return self._plan_locked(self._read_locked())

    def initialization_plan(self) -> dict[str, Any]:
        """Allow only a crash-truncated target set to finish before any live boundary."""
        with self._lock():
            state = self._read_locked()
            try:
                self._validate_state_boundary_locked(
                    state, Path(str(state.get("run_dir", "")))
                )
            except InvariantError:
                return {
                    "initializable": False,
                    "reason": "run-or-draft-boundary-invalid",
                }
            if state.get("safety_status") != "ALLOW":
                return {
                    "initializable": False,
                    "reason": "safety-not-terminal",
                }
            if not self._drafts_intact(state):
                return {
                    "initializable": False,
                    "reason": "draft-changed-or-missing",
                }
            pairs = state.get("pairs", {})
            required = self._required_pairs_for_state(state)
            pair_keys = set(pairs) if isinstance(pairs, dict) else set()
            active_keys = pair_keys & set(required)
            dormant_only_alias = (
                self._is_active_alias_state(state)
                and bool(pair_keys)
                and not active_keys
                and pair_keys <= {"x-article/en", "x-post/ja"}
            )
            # Active-four initialization records the four dormant skips before
            # registering the four live targets. If the worker is interrupted
            # in that publication-free window, the state is still a valid
            # partial initialization boundary; do not strand it as "no valid
            # incomplete run" before the first target can be registered.
            dormant_only_active_four = (
                not self._is_legacy_state(state)
                and bool(pair_keys)
                and not active_keys
                and pair_keys <= set(DORMANT_PAIRS)
            )
            dormant_only = dormant_only_alias or dormant_only_active_four
            if (
                not isinstance(pairs, dict)
                or not pairs
                or (not dormant_only and not active_keys)
                or (not dormant_only and not active_keys < set(required))
                or not pair_keys <= set(SUPPORTED_PAIRS)
                or any(
                    pair not in DORMANT_PAIRS
                    or not isinstance(pairs[pair], dict)
                    or (
                        pairs[pair].get("status") not in HISTORICAL_DORMANT_STATUSES
                        if self._is_active_alias_state(state)
                        and pair in {"zenn-article/ja", "devto/en"}
                        else pairs[pair].get("status") != "skipped"
                    )
                    for pair in pair_keys - set(required)
                )
            ):
                return {
                    "initializable": False,
                    "reason": "not-a-partial-target-set",
                }
            reinitialization_pairs = state.get("reinitialization_pairs", [])
            if reinitialization_pairs:
                if (
                    not isinstance(reinitialization_pairs, list)
                    or len(set(reinitialization_pairs))
                    != len(reinitialization_pairs)
                    or any(
                        pair not in required
                        for pair in reinitialization_pairs
                    )
                ):
                    return {
                        "initializable": False,
                        "reason": "invalid-destination-reinitialization-marker",
                    }
                missing = [pair for pair in required if pair not in pairs]
                marked = [
                    pair for pair in required if pair in reinitialization_pairs
                ]
                if missing != marked:
                    return {
                        "initializable": False,
                        "reason": "destination-reinitialization-marker-mismatch",
                    }
                return {
                    "initializable": True,
                    "reason": "recovered-destination-reinitialization",
                    "run_id": state["run_id"],
                    "run_dir": state["run_dir"],
                    "topic_id": state["topic_id"],
                    "initialization_pairs": marked,
                    "missing_dormant_skip_pairs": [
                        pair for pair in DORMANT_PAIRS if pair not in pairs
                    ],
                    "existing_pairs": {
                        pair: dict(pairs[pair])
                        for pair in required
                        if pair in pairs
                    },
                }
            run_rows = [
                row
                for row in self._ledger_rows_locked()
                if row.get("run_id") == state.get("run_id")
            ]
            if any(
                not (
                    _is_nonpublication_quality_audit(row, state)
                    or is_self_owned_publication_receipt(row, state)
                    or (
                        row.get("topic_id") == state.get("topic_id")
                        and f"{row.get('platform')}/{row.get('lang')}"
                        in required
                        and row.get("published") is False
                        and row.get("verified_logged_in") is False
                        and isinstance(row.get("state"), str)
                        and row["state"].startswith("unavailable:")
                        and not row.get("live_url")
                        and not row.get("receipt")
                        and not row.get("public_id")
                        and not row.get("published_at")
                    )
                )
                for row in run_rows
            ):
                return {
                    "initializable": False,
                    "reason": "run-ledger-boundary-exists",
                }
            try:
                for pair, entry in pairs.items():
                    status = entry.get("status")
                    if (
                        self._is_active_alias_state(state)
                        and pair in {"zenn-article/ja", "devto/en"}
                        and status in HISTORICAL_DORMANT_STATUSES
                    ):
                        continue
                    if entry.get("receipt"):
                        raise InvariantError("existing partial intent is not pristine")
                    if status == "skipped":
                        if (
                            pair not in DORMANT_PAIRS
                            or entry.get("skip_receipt", {}).get("slo")
                            != "not-applicable"
                        ):
                            raise InvariantError("invalid dormant skip receipt")
                        continue
                    if status == "unavailable":
                        target_kind = entry.get("target_kind")
                        target = entry.get("target")
                        if bool(target_kind) != bool(target):
                            raise InvariantError("partial unavailable target")
                        if target_kind and target:
                            validate_target(pair, str(target_kind), str(target))
                        continue
                    validate_target(
                        pair,
                        str(entry.get("target_kind", "")),
                        str(entry.get("target", "")),
                    )
                    if status != "intent":
                        raise InvariantError("existing partial intent is not pristine")
            except (AttributeError, InvariantError):
                return {
                    "initializable": False,
                    "reason": "existing-target-ambiguous",
                }
            missing = [pair for pair in required if pair not in pairs]
            return {
                "initializable": True,
                "reason": "partial-target-initialization",
                "run_id": state["run_id"],
                "run_dir": state["run_dir"],
                "topic_id": state["topic_id"],
                "initialization_pairs": missing,
                "missing_dormant_skip_pairs": [
                    pair for pair in DORMANT_PAIRS if pair not in pairs
                ],
                "existing_pairs": {
                    pair: dict(pairs[pair])
                    for pair in required
                    if pair in pairs
                },
            }

    def worker_plan(self) -> dict[str, Any]:
        """Return same-run pending work without the retired foreground replay budget."""
        with self._lock():
            return self._plan_locked(
                self._read_locked(), enforce_resume_budget=False
            )

    def completion_status(self) -> str:
        """Return complete/incomplete/ambiguous for the exact current run and topic."""
        with self._lock():
            state = self._read_locked()
            try:
                self._validate_state_boundary_locked(
                    state, Path(str(state.get("run_dir", "")))
                )
            except InvariantError:
                return "ambiguous"
            return self._current_ledger_status_locked(state)

    def begin_resume(self) -> dict[str, Any]:
        with self._lock():
            state = self._read_locked()
            plan = self._plan_locked(state)
            if not plan.get("resumable"):
                reason = str(plan.get("reason", "not-resumable"))
                if reason == "resume-budget-exhausted":
                    raise ResumeExhausted("resume budget exhausted")
                raise InvariantError(f"resume is not allowed: {reason}")
            attempts = int(state.get("resume_attempts", 0)) + 1
            maximum = int(state.get("max_resume_attempts", 0))
            state["resume_attempts"] = attempts
            self._write_locked(state)
            return {"attempt": attempts, "maximum": maximum}


def _store(args: argparse.Namespace) -> PublicationStore:
    return PublicationStore(Path(args.state), Path(args.ledger))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--ledger", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--run-id", required=True)
    init.add_argument("--run-dir", required=True)
    init.add_argument("--topic-id", required=True)
    init.add_argument("--draft-ja", required=True)
    init.add_argument("--draft-en", required=True)
    init.add_argument("--x-post-ja")
    init.add_argument("--headline-image", required=True)
    init.add_argument("--body-asset", action="append", required=True)
    init.add_argument("--safety", default="ALLOW", choices=("ALLOW", "BLOCK", "PENDING"))
    init.add_argument("--max-resume-attempts", default=3, type=int)
    init.add_argument("--require-quality", action="store_true")
    init.add_argument("--legacy-exact8", action="store_true")

    intent = sub.add_parser("intent")
    intent.add_argument("--pair", required=True, choices=SUPPORTED_PAIRS)
    intent.add_argument("--target-kind", required=True)
    intent.add_argument("--target", required=True)

    dormant_skip = sub.add_parser("dormant-skip")
    dormant_skip.add_argument("--pair", required=True, choices=DORMANT_PAIRS)
    dormant_skip.add_argument("--reason", default="dormant-destination")

    safety = sub.add_parser("safety")
    safety.add_argument("--status", required=True, choices=("ALLOW", "BLOCK", "PENDING"))

    guard = sub.add_parser("guard")
    guard.add_argument("--pair", required=True, choices=SUPPORTED_PAIRS)
    guard.add_argument("--remote-json", required=True)

    record = sub.add_parser("record-live")
    record.add_argument("--pair", required=True, choices=SUPPORTED_PAIRS)
    record.add_argument("--live-url", required=True)
    record.add_argument("--evidence-json", required=True)
    record.add_argument(
        "--test-local-asset-readback",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    sub.add_parser("plan")
    sub.add_parser("worker-plan")
    sub.add_parser("terminalize-invalid-x-post")
    sub.add_parser("begin-resume")
    args = parser.parse_args()
    store = _store(args)
    if args.command == "init":
        result = store.initialize(
            args.run_id,
            Path(args.run_dir),
            args.topic_id,
            {"ja": Path(args.draft_ja), "en": Path(args.draft_en)},
            args.safety,
            args.max_resume_attempts,
            Path(args.x_post_ja) if args.x_post_ja else None,
            Path(args.headline_image),
            [Path(path) for path in args.body_asset],
            require_quality=args.require_quality,
            legacy_exact8=args.legacy_exact8,
        )
    elif args.command == "intent":
        result = store.register_intent(args.pair, args.target_kind, args.target)
    elif args.command == "dormant-skip":
        result = store.register_dormant_skip(args.pair, args.reason)
    elif args.command == "safety":
        store.set_safety(args.status)
        result = {"status": args.status}
    elif args.command == "guard":
        result = store.guard(args.pair, json.loads(args.remote_json))
    elif args.command == "record-live":
        if args.test_local_asset_readback:
            resolved_state = Path(args.state).resolve(strict=False)
            if (
                os.environ.get("ARTICLE_TEST_ONLY") != "1"
                or not str(resolved_state).startswith(("/tmp/", "/private/tmp/"))
            ):
                raise InvariantError("test-only local asset readback is forbidden")
            globals()["fetch_remote_asset"] = (
                lambda _url, expected: Path(str(expected["path"])).read_bytes()
            )
        result = store.record_live(args.pair, args.live_url, json.loads(args.evidence_json))
    elif args.command == "plan":
        result = store.plan()
    elif args.command == "worker-plan":
        result = store.worker_plan()
    elif args.command == "terminalize-invalid-x-post":
        result = store.terminalize_invalid_x_post_length()
    else:
        result = store.begin_resume()
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InvariantError, ResumeExhausted) as error:
        print(f"REFUSED: {error}", file=os.sys.stderr)
        raise SystemExit(2)
