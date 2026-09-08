#!/usr/bin/env python3
"""Choose a safe start without replaying an unfinished or published run.

The launchd wrapper asks this before it allocates a new RUN_TS.  Decisions are based on
durable run/ledger artifacts, never wall-clock proximity to the 06:00 trigger.  A completed
run does not reserve the rest of the JST day: the caller may allocate a fresh run identity
for another topic.  Duplicate protection remains bound to the immutable run/topic/artifact
and publisher receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from publication_contract import ACTIVE_PAIRS
from publication_contract_resolver import (
    PublicationContractError,
    resolve_publication_contract,
)
from quarantine_invalid_run import QuarantineError, proof


REQUIRED = {
    ("note", "ja"),
    ("zenn-article", "ja"),
    ("devto", "en"),
    ("substack", "ja"),
    ("substack", "en"),
    ("x-article", "ja"),
    ("x-article", "en"),
    ("x-post", "ja"),
}
ACTIVE_REQUIRED = {
    (pair.split("/", 1)[0], pair.split("/", 1)[1]) for pair in ACTIVE_PAIRS
}
WITHOUT_ZENN = REQUIRED - {("zenn-article", "ja")}
PENDING_ZENN_STATUSES = {"pending", "live-recorded"}
X_READABILITY_ERROR_PREFIX = "x-article body media readability failed:"
X_EDIT_URL_RE = re.compile(r"https://x\.com/compose/articles/edit/[0-9]{8,}")
LEDGER_ALLOWED_KEYS = {
    "ts", "run_id", "topic_id", "topic", "platform", "lang", "live_url",
    "state", "verified_logged_in", "published", "reality_gate", "verified",
    "public_id", "published_at", "stable_target", "artifact_sha256", "language",
    "content_verified", "asset_hashes", "asset_urls", "asset_proofs",
    "asset_verified", "eyecatch_verified", "body_media_verified",
    "cover_verified", "timeline_verified", "emoji_verified", "status_id",
    "native_asset_count", "source", "readback_source", "destination_identity",
    "identity_verified", "identity_source",
}


def run_jst_date(run_id: str) -> str | None:
    if re.fullmatch(r"daily-[0-9]{4}-[0-9]{2}-[0-9]{2}", run_id):
        return run_id.removeprefix("daily-")
    try:
        stamp = datetime.strptime(run_id, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return stamp.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()


def run_order(run_id: str) -> tuple[datetime, str]:
    """Order mixed daily-* and UTC timestamp IDs by their actual start time."""
    if re.fullmatch(r"daily-[0-9]{4}-[0-9]{2}-[0-9]{2}", run_id):
        local_midnight = datetime.fromisoformat(
            f"{run_id.removeprefix('daily-')}T00:00:00"
        ).replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        return local_midnight.astimezone(timezone.utc), run_id
    stamp = datetime.strptime(run_id, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    return stamp, run_id


def ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def strict_ledger_rows(path: Path) -> list[dict[str, Any]] | None:
    """Parse the publication ledger without discarding damaged evidence."""
    if path.is_symlink():
        return None
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                return None
            rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None
    return rows


def validated_live_set(
    rows: list[dict[str, Any]], run_id: str, required: set[tuple[str, str]]
) -> tuple[bool, str | None]:
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from article_completion import validate_live_set  # pylint: disable=import-outside-toplevel

    exact, _, topic_id = validate_live_set(rows, run_id, required)
    return exact, topic_id


def validate_deferred_artifact(artifact: dict[str, Any], run_id: str, topic_id: str) -> None:
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    module_path = scripts / "zenn-deferred-control.py"
    spec = importlib.util.spec_from_file_location("article_zenn_deferred_control", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Zenn deferred validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.validate_artifact(
        artifact,
        Path(
            os.environ.get(
                "ZENN_REPO_PATH",
                str(Path.home() / ".local/state/life-manager/writer/checkouts/zenn-articles"),
            )
        ),
        run_id,
        topic_id,
    )


def valid_zenn_pending(path: Path, run_id: str, topic_id: str | None) -> bool:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    shape_valid = bool(
        isinstance(artifact, dict)
        and artifact.get("status") in PENDING_ZENN_STATUSES
        and artifact.get("run_id") == run_id
        and artifact.get("topic_id") == topic_id
        and isinstance(artifact.get("slug"), str)
        and re.fullmatch(r"[a-z0-9_-]{12,50}", artifact["slug"])
        and isinstance(artifact.get("title"), str)
        and artifact["title"].strip()
        and isinstance(artifact.get("markdown_file"), str)
        and artifact["markdown_file"]
        and isinstance(artifact.get("handed_off_at"), str)
        and artifact["handed_off_at"]
        and artifact.get("live_url") == f"https://zenn.dev/{os.environ.get('ZENN_ACCOUNT', '')}/articles/{artifact['slug']}"
    )
    if not shape_valid or topic_id is None:
        return False
    try:
        validate_deferred_artifact(artifact, run_id, topic_id)
    except Exception:
        return False
    return True


def publication_plan(state_path: Path, ledger_path: Path) -> dict[str, Any]:
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from publication_resume import PublicationStore  # pylint: disable=import-outside-toplevel

    return PublicationStore(state_path, ledger_path).plan()


def unavailable_x_readability_release(
    state_dir: Path, run_dir: Path, run_id: str, rows: list[dict[str, Any]]
) -> bool:
    """Release one active-four run only after a proof-bound X media failure.

    The three revenue receipts must be intact and the X pair must be an exact,
    no-effect unavailable terminal.  This is deliberately stricter than the
    normal resume plan so a forged receipt cannot authorize another article.
    """
    state_path = run_dir / "gates" / "publication-state.json"
    if run_dir.is_symlink() or not run_dir.is_dir() or state_path.is_symlink():
        return False
    state = _regular_json(state_path)
    if (
        state is None
        or state.get("publication_contract") != "active-four"
        or state.get("run_id") != run_id
        or not isinstance(state.get("topic_id"), str)
        or not state["topic_id"].strip()
    ):
        return False
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from publication_resume import PublicationStore, validate_receipt_evidence

        state = PublicationStore(
            state_path, state_dir / "articles.jsonl"
        ).validate_managed_boundary(run_dir)
    except Exception:
        return False

    pairs = state.get("pairs")
    if not isinstance(pairs, dict):
        return False
    x_entry = pairs.get("x-article/ja")
    allowed_x_keys = {
        "platform", "lang", "target_kind", "target", "status", "intent_at",
        "error", "unavailable_at",
    }
    if not (
        isinstance(x_entry, dict)
        and set(x_entry) <= allowed_x_keys
        and x_entry.get("platform") == "x-article"
        and x_entry.get("lang") == "ja"
        and x_entry.get("status") == "unavailable"
        and x_entry.get("target_kind") == "x-draft-url"
        and isinstance(x_entry.get("target"), str)
        and X_EDIT_URL_RE.fullmatch(x_entry["target"])
        and isinstance(x_entry.get("error"), str)
        and x_entry["error"].startswith(X_READABILITY_ERROR_PREFIX)
        and all(
            x_entry.get(key) is None or x_entry.get(key) == ""
            for key in ("receipt", "live_url", "public_id", "published_at")
        )
    ):
        return False

    readability = _regular_json(
        run_dir / "gates" / "x-inplace-repair" / "ja" / "media-readability.json"
    )
    body_assets = state.get("media", {}).get("body_assets")
    if not (
        isinstance(readability, dict)
        and readability.get("version") == 1
        and readability.get("status") == "FAIL"
        and readability.get("render_width") == 587
        and readability.get("min_height") == 110
        and readability.get("max_height") == 650
        and isinstance(readability.get("violations"), list)
        and readability["violations"]
        and any(
            isinstance(item, str) and (item.startswith("too-flat:") or item.startswith("too-tall:"))
            for item in readability["violations"]
        )
        and isinstance(body_assets, list)
        and body_assets
        and isinstance(readability.get("images"), list)
        and len(readability["images"]) == len(body_assets)
    ):
        return False
    expected_media: dict[Path, str] = {}
    for item in body_assets:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return False
        try:
            expected_media[Path(item["path"]).resolve(strict=True)] = str(item["sha256"])
        except (KeyError, OSError, RuntimeError):
            return False
    seen_media: set[Path] = set()
    for item in readability["images"]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return False
        try:
            path = Path(item["path"]).resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        if path.parent != run_dir.resolve() or path in seen_media or path not in expected_media:
            return False
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return False
        if digest != expected_media[path] or item.get("sha256") != digest:
            return False
        seen_media.add(path)
    if seen_media != set(expected_media):
        return False
    try:
        repair_path = scripts / "x-publish" / "x_inplace_repair.py"
        if str(repair_path.parent) not in sys.path:
            sys.path.insert(0, str(repair_path.parent))
        repair_spec = importlib.util.spec_from_file_location(
            "writer_x_inplace_repair_for_start", repair_path
        )
        if repair_spec is None or repair_spec.loader is None:
            return False
        repair_module = importlib.util.module_from_spec(repair_spec)
        repair_spec.loader.exec_module(repair_module)
        recomputed = repair_module._body_media_readability(
            [Path(item["path"]).resolve(strict=True) for item in body_assets]
        )
    except Exception:
        return False
    media_receipt_keys = (
        "version", "status", "min_height", "max_height", "render_width",
        "images", "violations",
    )
    if any(readability.get(key) != recomputed.get(key) for key in media_receipt_keys):
        return False
    if not (
        readability.get("run_id") == run_id
        and readability.get("pair") == "x-article/ja"
        and readability.get("target") == x_entry.get("target")
        and readability.get("target_kind") == "x-draft-url"
        and readability.get("readback_status") == "not-live"
        and readability.get("readback_verified") is True
        and readability.get("content_verified") is True
        and readability.get("artifact_sha256")
        == state.get("drafts", {}).get("ja", {}).get("sha256")
        and readability.get("destination_identity")
        == state.get("destination_identities", {}).get("x-article/ja")
        and readability.get("identity_verified") is True
        and readability.get("identity_source") == "x-authenticated-edit-url"
    ):
        return False

    revenue_pairs = {"note/ja", "substack/ja", "substack/en"}
    seen_pairs: set[str] = set()
    try:
        from publication_remote import probe
    except Exception:
        return False
    x_readback = probe("x-article/ja", str(x_entry["target"]), state)
    if not (
        isinstance(x_readback, dict)
        and x_readback.get("status") == "not-live"
        and x_readback.get("verified") is True
        and x_readback.get("target") == x_entry.get("target")
        and x_readback.get("content_verified") is True
        and x_readback.get("artifact_sha256")
        == state.get("drafts", {}).get("ja", {}).get("sha256")
        and x_readback.get("destination_identity")
        == state.get("destination_identities", {}).get("x-article/ja")
        and x_readback.get("identity_verified") is True
        and x_readback.get("identity_source") == "x-authenticated-edit-url"
    ):
        return False
    for row in rows:
        if row.get("run_id") != run_id:
            continue
        pair = f"{row.get('platform', '')}/{row.get('lang', '')}"
        if pair not in revenue_pairs or row.get("topic_id") != state.get("topic_id"):
            return False
        if not set(row) <= LEDGER_ALLOWED_KEYS:
            return False
        if pair in seen_pairs:
            return False
        if row.get("published") is not True or row.get("reality_gate") != "PASS":
            return False
        if any(
            row.get(key) not in (None, "", False, 0)
            for key in ("effect", "money", "amount", "payment", "charge")
        ):
            return False
        entry = pairs.get(pair)
        receipt = entry.get("receipt") if isinstance(entry, dict) else None
        evidence = receipt.get("evidence") if isinstance(receipt, dict) else None
        live_url = receipt.get("live_url") if isinstance(receipt, dict) else None
        if not (
            isinstance(entry, dict)
            and entry.get("status") == "live"
            and isinstance(receipt, dict)
            and isinstance(evidence, dict)
            and isinstance(live_url, str)
            and row.get("live_url") == live_url
        ):
            return False
        remote = probe(pair, str(entry.get("target", "")), state)
        if not (
            isinstance(remote, dict)
            and remote.get("status") == "live"
            and remote.get("verified") is True
            and remote.get("live_url") == live_url
            and remote.get("public_id")
            and remote.get("content_verified") is True
            and remote.get("identity_verified") is True
            and remote.get("destination_identity")
            == state.get("destination_identities", {}).get(pair)
            and isinstance(remote.get("identity_source"), str)
            and remote.get("asset_verified") is True
            and remote.get("body_media_verified") is True
            and (
                (
                    pair == "note/ja"
                    and remote.get("monetization_verified") is True
                    and remote.get("price") == 500
                )
                or (
                    pair.startswith("substack/")
                    and remote.get("monetization_verified") is True
                    and remote.get("audience") == "only_paid"
                    and remote.get("paywall_verified") is True
                )
            )
        ):
            return False
        try:
            for candidate in (evidence, remote, row):
                validate_receipt_evidence(
                    state, pair, live_url, candidate, reread_remote_assets=False
                )
        except Exception:
            return False
        protected_receipt_keys = (
            "verified", "public_id", "published_at", "stable_target",
            "artifact_sha256", "language", "content_verified", "asset_hashes",
            "asset_urls", "asset_proofs", "asset_verified", "body_media_verified",
            "eyecatch_verified", "destination_identity", "identity_verified",
            "identity_source",
        )
        if any(
            key in evidence and evidence.get(key) != remote.get(key)
            for key in protected_receipt_keys
        ):
            return False
        if any(
            key in remote and (key not in row or row.get(key) != remote.get(key))
            for key in protected_receipt_keys
        ):
            return False
        seen_pairs.add(pair)
    return seen_pairs == revenue_pairs and len(seen_pairs) == 3


def generation_resume_plan(run_dir: Path, ledger_path: Path) -> dict[str, Any]:
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from article_generation_state import resume_decision  # pylint: disable=import-outside-toplevel

    return resume_decision(
        run_dir,
        run_dir.name,
        run_dir / "article-daily-prompt.txt",
        ledger_path,
    )


def quality_repair_ready_owner(run_dir: Path, ledger_path: Path) -> bool:
    state = _regular_json(run_dir / "gates" / "generation-state.json")
    if not state or state.get("status") != "quality-repair-ready":
        return False
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from article_generation_state import adopt_prepublication  # pylint: disable=import-outside-toplevel

    try:
        result = adopt_prepublication(
            run_dir,
            run_dir.name,
            run_dir / "article-daily-prompt.txt",
            ledger_path,
        )
    except Exception:
        return False
    return result == {"action": "unchanged", "status": "quality-repair-ready"}


def _regular_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def persisted_publication_contract(run_dir: Path) -> str | None:
    """Read the run's explicit contract before classifying a completed ledger.

    The active-four subset is not evidence that a legacy exact-eight run is
    complete: an old run can have published the same four current destinations
    while its four legacy destinations are still pending.  Missing or malformed
    state therefore stays fail-closed instead of being treated as a new run.
    """

    state = _regular_json(run_dir / "gates" / "publication-state.json")
    if state is None:
        return None
    try:
        return resolve_publication_contract(
            run_dir / "gates" / "publication-state.json",
            run_dir.parent.parent / "articles.jsonl",
            run_dir.name,
            state=state,
        )
    except (PublicationContractError, TypeError, ValueError):
        return None


def _preflight_only_run(run_dir: Path, rows: list[dict[str, Any]], run_id: str) -> bool:
    """Allow a same-day retry when the earlier wake never reached generation.

    A demand-authority miss happens after the wrapper has created the run record and
    consumed the baseline strategy.  That record contains no prompt, draft, gate,
    publication state, or ledger row, so reusing its stable daily id cannot replay an
    external side effect.  Any additional artifact keeps the normal fail-closed
    classification below.
    """

    if run_dir.is_symlink() or not run_dir.is_dir():
        return False
    if any(row.get("run_id") == run_id for row in rows):
        return False
    git_hash = run_dir / "git-hash.txt"
    gates = run_dir / "gates"
    strategy_path = gates / "strategy-consumption.json"
    if (
        git_hash.is_symlink()
        or not git_hash.is_file()
        or gates.is_symlink()
        or not gates.is_dir()
        or strategy_path.is_symlink()
        or not strategy_path.is_file()
    ):
        return False
    strategy = _regular_json(run_dir / "gates" / "strategy-consumption.json")
    if not (
        strategy is not None
        and strategy.get("run_id") == run_id
        and strategy.get("status") == "baseline"
        and strategy.get("versions") == []
    ):
        return False
    # Enumerate every direct descendant rather than only regular files.  A symlink,
    # FIFO, socket, empty nested directory, or unexpected regular file is evidence
    # that the run progressed beyond the harmless preflight boundary.
    if {
        child.name for child in run_dir.iterdir()
    } != {"git-hash.txt", "gates"}:
        return False
    return {
        child.name for child in gates.iterdir()
    } == {"strategy-consumption.json"}


def _exhausted_prepublication_archive(
    state_dir: Path, run_dir: Path, run_id: str, rows: list[dict[str, Any]],
) -> bool:
    """Release a run only after its bounded generation archive is complete.

    A timeout archive can move the immutable prompt and generation state out of
    the run directory.  Once all charged attempts are exhausted, that old run
    cannot be resumed; a new identity is safe only when the run contains no
    publication/ledger row and the latest archive has either control evidence
    only or the complete content artifact set, with no symlink or
    publication-state file.
    """
    for row in rows:
        if row.get("run_id") != run_id:
            continue
        if (
            row.get("published") is True
            or bool(row.get("draft_url"))
            or bool(row.get("live_url"))
            or row.get("state") == "live"
            or row.get("reality_gate") == "PASS"
        ):
            return False
    if run_dir.is_symlink() or not run_dir.is_dir():
        return False
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from article_generation_state import prepublication_empty  # pylint: disable=import-outside-toplevel

    safe, _ = prepublication_empty(run_dir, run_id, state_dir / "articles.jsonl")
    if not safe:
        return False
    archive_parent = state_dir / "interrupted-generation" / run_id
    attempts = sorted(
        (
            path for path in archive_parent.iterdir()
            if path.is_dir() and path.name.startswith("attempt-")
            and path.name.removeprefix("attempt-").isdigit()
        ),
        key=lambda path: int(path.name.removeprefix("attempt-")),
    ) if archive_parent.is_dir() and not archive_parent.is_symlink() else []
    if not attempts:
        return False
    latest = attempts[-1]
    required = ("generation-state.json", "generation-exhaustion-receipt.json")
    if any(
        not (latest / relative).is_file()
        or (latest / relative).is_symlink()
        for relative in required
    ):
        return False
    archived_state = _regular_json(latest / "generation-state.json")
    receipt = _regular_json(latest / "generation-exhaustion-receipt.json")
    if archived_state is None or receipt is None:
        return False
    attempts_state = archived_state.get("attempts")
    if (
        archived_state.get("version") != 1
        or archived_state.get("run_id") != run_id
        or archived_state.get("status") != "interrupted-safe"
        or not isinstance(archived_state.get("prompt_sha256"), str)
        or not isinstance(attempts_state, list)
        or not attempts_state
    ):
        return False
    final = attempts_state[-1]
    if (
        not isinstance(final, dict)
        or final.get("status") != "interrupted-safe"
        or final.get("return_code") not in {75, 124, 130, 143}
        or not isinstance(final.get("archive_manifest"), list)
        or (
            final.get("return_code") == 75
            and (
                not isinstance(final.get("provider_return_code"), int)
                or final["provider_return_code"] == 0
            )
        )
    ):
        return False
    content = (
        "article-en.md", "article-ja.md", "headline-image.png",
        "body-diagram.png",
    )
    manifest_paths = set()
    for item in final["archive_manifest"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item["path"], str)
            or not isinstance(item["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        ):
            return False
        relative = Path(item["path"])
        archived = latest / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != item["path"]
            or archived.is_symlink()
            or not archived.is_file()
            or hashlib.sha256(archived.read_bytes()).hexdigest() != item["sha256"]
        ):
            return False
        manifest_paths.add(item["path"])
    if manifest_paths.intersection(content):
        if not set(content).issubset(manifest_paths):
            return False
        if any(not (latest / path).is_file() or (latest / path).is_symlink() for path in content):
            return False
    maximum = archived_state.get("maximum_attempts")
    if not isinstance(maximum, int) or maximum < 1:
        return False
    empty = sum(
        1 for item in attempts_state
        if isinstance(item, dict)
        and item.get("status") == "interrupted-safe"
        and item.get("archive_manifest") == []
    )
    charged = len(attempts_state) - min(
        empty, int(archived_state.get("maximum_empty_interruption_recoveries", 0))
    )
    manifest_hash = hashlib.sha256(
        json.dumps(
            final["archive_manifest"], ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    state_hash = hashlib.sha256(
        (latest / "generation-state.json").read_bytes()
    ).hexdigest()
    if receipt != {
        "schema": "writer.generation-exhaustion-receipt",
        "version": 1,
        "run_id": run_id,
        "attempt": final.get("attempt"),
        "status": "interrupted-safe",
        "return_code": final.get("return_code"),
        "charged_attempts": charged,
        "maximum_attempts": maximum,
        "state_sha256": state_hash,
        "archive_manifest_sha256": manifest_hash,
        "publication_state_absent": True,
        "public_ledger_rows": 0,
    }:
        return False
    return not any(
        path.name == "publication-state.json"
        for path in latest.rglob("*")
        if path.is_file() or path.is_symlink()
    )


def _unpublished_quality_audit(row: dict[str, Any]) -> bool:
    state = row.get("state")
    legacy = row.get("platform") == "quality" and row.get("published") is False
    current = bool(
        row.get("platform") is None
        and row.get("lang") in {"ja", "en"}
        and state == "quality-blocked:block_freeze"
        and row.get("published") is False
        and row.get("verified_logged_in") is False
        and row.get("draft_url") is None
        and row.get("live_url") is None
        and isinstance(row.get("topic_id"), str)
        and row.get("topic_id", "").strip()
        and row.get("topic_source") == "paid-demand"
        and isinstance(row.get("editorial_form"), str)
        and row.get("editorial_form", "").strip()
    )
    return legacy or current


def _quality_failure_feedback(
    gates: Path,
    run_id: str,
    language_quality: dict[str, Any],
) -> dict[str, Any] | None:
    """Return hash-bound actionable feedback from current terminal receipts."""

    items: list[dict[str, str]] = []
    receipt_sha256: dict[str, str] = {}
    article_sha256: dict[str, str] = {}
    for lang in ("ja", "en"):
        quality = language_quality.get(lang)
        if not isinstance(quality, dict):
            return None
        digest = quality.get("article_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return None
        article_sha256[lang] = digest

        editorial_path = gates / f"editorial-{lang}.json"
        reader_path = gates / f"reader-testing-gate-{lang}.terminal.json"
        editorial = _regular_json(editorial_path)
        reader = _regular_json(reader_path)
        if (
            editorial is None
            or editorial.get("article_sha256") != digest
            or reader is None
            or reader.get("article_sha256") != digest
        ):
            return None
        receipt_sha256[f"editorial-{lang}"] = hashlib.sha256(
            editorial_path.read_bytes()
        ).hexdigest()
        receipt_sha256[f"reader-{lang}"] = hashlib.sha256(
            reader_path.read_bytes()
        ).hexdigest()

        if quality.get("editorial") == "FAIL":
            fixes = editorial.get("fixes")
            if not isinstance(fixes, list):
                return None
            editorial_items = [
                value.strip()
                for value in fixes
                if isinstance(value, str) and value.strip()
            ]
            if not editorial_items:
                return None
            items.extend(
                {
                    "id": f"editorial-{lang}-{index}",
                    "lang": lang,
                    "kind": "editorial_fix",
                    "text": text,
                }
                for index, text in enumerate(editorial_items, start=1)
            )

        if quality.get("reader") == "FAIL":
            payload = reader.get("payload")
            questions = (
                payload.get("unanswered_questions")
                if isinstance(payload, dict)
                else None
            )
            if not isinstance(questions, list):
                return None
            reader_items = [
                value.strip()
                for value in questions
                if isinstance(value, str) and value.strip()
            ]
            if not reader_items:
                return None
            items.extend(
                {
                    "id": f"reader-{lang}-{index}",
                    "lang": lang,
                    "kind": "reader_question",
                    "text": text,
                }
                for index, text in enumerate(reader_items, start=1)
            )
    if not items:
        return None
    feedback: dict[str, Any] = {
        "version": 1,
        "source_run_id": run_id,
        "article_sha256": article_sha256,
        "receipt_sha256": receipt_sha256,
        "items": items,
    }
    feedback["feedback_sha256"] = hashlib.sha256(
        json.dumps(
            feedback,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return feedback


def terminal_quality_finished_at(
    run_dir: Path,
    run_id: str,
    rows: list[dict[str, Any]],
) -> tuple[datetime, str, str, dict[str, Any]] | None:
    """Return a trusted terminal timestamp for an unpublished quality block."""
    gates = run_dir / "gates"
    publication_state = gates / "publication-state.json"
    if (
        run_dir.is_symlink()
        or gates.is_symlink()
        or not gates.is_dir()
        or publication_state.exists()
        or publication_state.is_symlink()
    ):
        return None
    # A terminal quality block deliberately writes one unpublished `quality`
    # carry-over row so the rejected topic and feedback remain auditable.  That
    # bookkeeping row is not a publication side effect and must not poison the
    # one bounded quality-replacement path.  Any destination row or published
    # row remains fail-closed because it may represent an irreversible remote
    # action even when the publication-state receipt is missing.
    if any(
        row.get("run_id") == run_id
        and not _unpublished_quality_audit(row)
        for row in rows
    ):
        return None

    terminal_path = gates / "terminal-quality-blocked.json"
    terminal = _regular_json(terminal_path)
    quality_path = gates / "quality-self-heal.json"
    blocker_path = gates / "quality-repair-blocker.json"
    route = _regular_json(gates / "topic-route.json")
    if terminal is not None:
        drafts = terminal.get("drafts")
        if not (
            terminal.get("version") == 1
            and terminal.get("status") == "terminal_quality_blocked"
            and terminal.get("run_id") == run_id
            and terminal.get("publication") in {None, "not_started"}
            and isinstance(drafts, dict)
            and quality_path.is_file()
            and blocker_path.is_file()
            and terminal.get("quality_sha256") == hashlib.sha256(quality_path.read_bytes()).hexdigest()
            and terminal.get("blocker_sha256") == hashlib.sha256(blocker_path.read_bytes()).hexdigest()
            and route
            and terminal.get("topic_id") == route.get("topic_id")
            and terminal.get("editorial_form") == route.get("editorial_form")
            and all(
                (run_dir / f"article-{lang}.md").is_file()
                and not (run_dir / f"article-{lang}.md").is_symlink()
                and drafts.get(lang) == hashlib.sha256((run_dir / f"article-{lang}.md").read_bytes()).hexdigest()
                for lang in ("ja", "en")
            )
            and isinstance(terminal.get("quality_failure_feedback"), dict)
        ):
            return None
        try:
            finished = datetime.fromisoformat(str(terminal["created_at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return None
        if finished.tzinfo is None:
            return None
        return (
            finished.astimezone(timezone.utc),
            str(terminal["topic_id"]),
            str(terminal["editorial_form"]),
            terminal["quality_failure_feedback"],
        )

    quality = _regular_json(gates / "quality-self-heal.json")
    generation = _regular_json(gates / "generation-state.json")
    route = _regular_json(gates / "topic-route.json")
    quality_advisory = bool(
        quality
        and quality.get("version") == 2
        and quality.get("action") == "ready_to_freeze"
        and quality.get("quality_advisory") is True
        and quality.get("attempt") == 1
        and quality.get("publication_policy") == "continuous"
    )
    if (
        quality is None
        or quality.get("version") != 2
        or (
            not quality_advisory
            and (
                quality.get("action") != "block_freeze"
                or int(quality.get("attempt", 0)) < 2
            )
        )
        or generation is None
        or generation.get("version") != 1
        or generation.get("run_id") != run_id
        or generation.get("status") != "provider-returned"
        or route is None
        or not isinstance(route.get("topic_id"), str)
        or not route["topic_id"].strip()
        or not isinstance(route.get("editorial_form"), str)
        or not route["editorial_form"].strip()
    ):
        return None

    language_quality = quality.get("quality")
    if not isinstance(language_quality, dict):
        return None
    failed_languages = quality.get("failed_languages")
    if failed_languages is None:
        # Version-2 historical terminal receipts predate the explicit summary;
        # derive it only from their hash-bound per-language readiness.
        failed_languages = [
            lang
            for lang in ("ja", "en")
            if isinstance(language_quality.get(lang), dict)
            and language_quality[lang].get("ready") is False
        ]
    if (
        not isinstance(failed_languages, list)
        or not failed_languages
        or any(lang not in {"ja", "en"} for lang in failed_languages)
        or len(set(failed_languages)) != len(failed_languages)
    ):
        return None
    failed_set = set(failed_languages)
    for lang in ("ja", "en"):
        record = language_quality.get(lang)
        article = run_dir / f"article-{lang}.md"
        if (
            not isinstance(record, dict)
            or article.is_symlink()
            or not article.is_file()
            or record.get("article_sha256")
            != hashlib.sha256(article.read_bytes()).hexdigest()
            or (
                record.get("evaluation_current") is not True
                and not quality_advisory
            )
            or record.get("identity_current") is not True
        ):
            return None
        identity_path = gates / f"identity-{lang}.json"
        identity = _regular_json(identity_path)
        conscience_path = gates / f"conscience-{lang}.json"
        conscience = _regular_json(conscience_path)
        if not (
            identity is not None
            and identity.get("verdict") == "PASS"
            and identity.get("article_sha256") == record.get("article_sha256")
            and conscience is not None
            and conscience.get("verdict") == "ALLOW"
            and conscience.get("reasons") == []
        ):
            return None
        if lang in failed_set:
            if record.get("ready") is not False or (
                record.get("editorial") != "FAIL"
                and record.get("reader") != "FAIL"
            ):
                return None
        elif (
            record.get("ready") is not True
            or record.get("editorial") != "PASS"
            or record.get("reader") != "PASS"
        ):
            return None
    feedback = _quality_failure_feedback(gates, run_id, language_quality)
    if feedback is None:
        return None

    repair = _regular_json(gates / "quality-repair-state.json")
    if repair is not None and (
        repair.get("status") != "terminal-blocked"
        or repair.get("attempts") not in {1, 2}
        or not isinstance(repair.get("return_code"), int)
    ):
        return None

    attempts = generation.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    final_attempt = attempts[-1]
    if (
        not isinstance(final_attempt, dict)
        or final_attempt.get("status") != "provider-returned"
        or final_attempt.get("return_code") != 0
        or not isinstance(final_attempt.get("finished_at"), str)
    ):
        return None
    try:
        finished = datetime.fromisoformat(
            final_attempt["finished_at"].replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if finished.tzinfo is None:
        return None
    return (
        finished.astimezone(timezone.utc),
        route["topic_id"].strip(),
        route["editorial_form"].strip(),
        feedback,
    )


def decide(state_dir: Path | str, local_date: str) -> dict[str, str]:
    state_dir = Path(state_dir)
    runs_dir = state_dir / "runs"
    ledger = state_dir / "articles.jsonl"
    rows = strict_ledger_rows(ledger)
    if rows is None:
        return {"action": "block-incomplete", "run_id": "", "reason": "ledger-invalid"}
    if not runs_dir.is_dir():
        return {"action": "new", "reason": "no-same-jst-day-run"}

    run_ids = sorted(
        (
            path.name
            for path in runs_dir.iterdir()
            if path.is_dir() and run_jst_date(path.name) == local_date
        ),
        key=run_order,
        reverse=True,
    )
    if not run_ids:
        return {"action": "new", "reason": "no-same-jst-day-run"}

    # Only the newest same-day run can own an unfinished obligation.  Once its
    # persisted contract is complete, return `new` so the wrapper can allocate
    # a fresh run identity for another article today. Looking through a newer
    # partial/ambiguous run would still hide unsafe publication state.
    run_id = run_ids[0]
    run_dir = runs_dir / run_id
    contract = persisted_publication_contract(run_dir)
    exact_eight, _ = validated_live_set(rows, run_id, REQUIRED)
    if contract == "legacy-exact8" and exact_eight:
        return {
            "action": "new",
            "run_id": "",
            "previous_run_id": run_id,
            "reason": "new-after-complete:legacy-exact8",
        }
    # Only an explicit active-four publication state can release the same-day
    # start gate after four receipts.  A legacy exact-eight run with the same
    # four rows is still incomplete and must remain resumable/blocking.
    if contract == "active-four":
        active_four, _ = validated_live_set(rows, run_id, ACTIVE_REQUIRED)
        if active_four:
            return {
                "action": "new",
                "run_id": "",
                "previous_run_id": run_id,
                "reason": "new-after-complete:active-four",
            }
    exact_seven, topic_id = validated_live_set(rows, run_id, WITHOUT_ZENN)
    pending = run_dir / "gates" / "zenn-deferred.json"
    if exact_seven and valid_zenn_pending(pending, run_id, topic_id):
        return {
            "action": "skip-zenn-worker",
            "run_id": run_id,
            "reason": "same-jst-day-exact7-valid-pending",
        }

    # Recompute the immutable invalid-media/no-live proof at every decision.  The
    # optional quarantine receipt is audit evidence only; a hand-written receipt
    # cannot release the gate without this fresh proof.
    try:
        proof(state_dir, run_id)
    except (OSError, QuarantineError):
        pass
    else:
        return {
            "action": "new",
            "run_id": "",
            "previous_run_id": run_id,
            "reason": "same-jst-day-invalid-media-proof",
        }

    if unavailable_x_readability_release(state_dir, run_dir, run_id, rows):
        return {
            "action": "new",
            "run_id": "",
            "previous_run_id": run_id,
            "reason": "same-jst-day-unavailable-x-readability",
        }

    state_path = run_dir / "gates" / "publication-state.json"
    if state_path.is_file() and not state_path.is_symlink():
        try:
            plan = publication_plan(state_path, ledger)
        except Exception:  # an unreadable/ambiguous irreversible state must never create a new article
            return {"action": "block-incomplete", "run_id": run_id, "reason": "same-jst-day-state-invalid"}
        if plan.get("resumable") is True:
            return {
                "action": "skip-pending-worker",
                "run_id": run_id,
                "reason": "same-jst-day-owned-by-pending-worker",
            }
        return {
            "action": "block-incomplete",
            "run_id": run_id,
            "reason": f"same-jst-day-not-resumable:{plan.get('reason', 'unknown')}",
        }

    generation = generation_resume_plan(run_dir, ledger)
    if generation.get("resumable") is True:
        if generation.get("status") == "interrupted-safe":
            reason = "same-jst-day-prepublication-interruption"
        elif generation.get("status") == "uninitialized-safe":
            reason = "same-jst-day-prepublication-uninitialized"
        else:
            reason = "same-jst-day-prepublication-provider-failure"
        return {
            "action": "resume-generation",
            "run_id": run_id,
            "reason": reason,
        }
    if quality_repair_ready_owner(run_dir, ledger):
        return {
            "action": "skip-pending-worker",
            "run_id": run_id,
            "reason": "same-jst-day-owned-by-quality-repair",
        }
    if _exhausted_prepublication_archive(state_dir, run_dir, run_id, rows):
        return {
            "action": "new",
            "run_id": "",
            "previous_run_id": run_id,
            "reason": "same-jst-day-exhausted-prepublication-archive",
        }
    if _preflight_only_run(run_dir, rows, run_id):
        return {
            "action": "new",
            "run_id": run_id,
            "reason": "same-jst-day-preflight-only-run",
        }
    generation_state = _regular_json(
        run_dir / "gates" / "generation-state.json"
    )
    quality = _regular_json(run_dir / "gates" / "quality-self-heal.json")
    records = quality.get("quality") if quality else None
    quality_reroute = bool(
        generation_state
        and generation_state.get("status") == "provider-returned"
        and quality
        and quality.get("version") == 2
        and quality.get("attempt") == 1
        and quality.get("action") == "reroute"
        and isinstance(records, dict)
        and not any(
            row.get("run_id") == run_id and row.get("published") is True
            for row in rows
        )
        and all(
            (run_dir / f"article-{lang}.md").is_file()
            and not (run_dir / f"article-{lang}.md").is_symlink()
            and records.get(lang, {}).get("article_sha256")
            == hashlib.sha256(
                (run_dir / f"article-{lang}.md").read_bytes()
            ).hexdigest()
            for lang in ("ja", "en")
        )
    )
    if quality_reroute:
        return {
            "action": "resume-generation",
            "run_id": run_id,
            "reason": "same-jst-day-quality-reroute",
        }
    quality_terminal = terminal_quality_finished_at(run_dir, run_id, rows)
    if quality_terminal is not None:
        (
            quality_finished,
            forbidden_topic_id,
            forbidden_editorial_form,
            quality_failure_feedback,
        ) = quality_terminal
        if len(run_ids) >= 2:
            return {
                "action": "skip-quality-miss",
                "run_id": run_id,
                "reason": "same-jst-day-quality-replacement-limit",
            }
        replacement_id = quality_finished.strftime("%Y%m%d-%H%M%S")
        if run_jst_date(replacement_id) != local_date:
            return {
                "action": "block-incomplete",
                "run_id": run_id,
                "reason": "same-jst-day-quality-terminal-time-mismatch",
            }
        return {
            "action": "new-quality-replacement",
            "run_id": replacement_id,
            "replaced_run_id": run_id,
            "forbidden_topic_id": forbidden_topic_id,
            "forbidden_editorial_form": forbidden_editorial_form,
            "quality_failure_feedback": quality_failure_feedback,
            "reason": "same-jst-day-terminal-quality-block",
        }
    return {"action": "block-incomplete", "run_id": run_id, "reason": "same-jst-day-unclassified-run"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--local-date", required=True)
    args = parser.parse_args()
    print(json.dumps(decide(Path(args.state_dir), args.local_date), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
