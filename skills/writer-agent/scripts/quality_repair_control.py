#!/usr/bin/env python3
"""Bounded repair for exact unpublished quality blocks after source fixes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import article_generation_state as generation_state
from article_generation_state import ledger_has_public_effect


CURRENT_QUALITY_VERSION = 2
REPAIR_EPOCH = 1
MAX_REPAIR_ATTEMPTS = 2
QUALITY_REPAIR_EVIDENCE_INVALID = "quality-repair-evidence-invalid"
READER_TRACEBACK_SUFFIX = (
    "QualitySelfHealError: quality reader receipt is missing for ja"
)
SOURCE_RECOVERY_REASON = "tracked-active-editorial-repair-source-defect"
SOURCE_RECOVERY_ERROR_SUFFIX = (
    "QualitySelfHealError: quality receipt snapshot hash binding failed"
)
OWNER_RECOVERY_REASON = "tracked-repair-owner-prompt-source-defect"
OWNER_PROMPT_PROHIBITION = (
    "ARTICLE_QUALITY_REPAIR_OWNER_PID is already set by the wrapper. Never export, "
    "unset, or overwrite it; never assign $$ or $PPID. Every gate command must "
    "inherit the exact existing value."
)
QUALITY_SELF_HEAL_ORPHAN_OCCURRENCE_SHA256 = (
    "42a50e9b7f7d5d49889326b27bbdfeeab06bef7760d309ad927315edee55ab61"
)


class QualityRepairError(ValueError):
    """The run is not an exact bounded quality-repair candidate."""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _regular_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    return _read_json(path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _receipt_hash(value: dict[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    return hashlib.sha256(
        json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_create_once(path: Path, value: dict[str, Any]) -> str:
    """Create a complete regular receipt without ever replacing an existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise QualityRepairError("quality-repair-source-recovery-exists") from exc
        temporary_stat = os.lstat(temporary)
        destination_stat = os.lstat(path)
        if not (
            os.path.isfile(temporary)
            and os.path.isfile(path)
            and temporary_stat.st_dev == destination_stat.st_dev
            and temporary_stat.st_ino == destination_stat.st_ino
            and temporary_stat.st_nlink == destination_stat.st_nlink == 2
        ):
            raise QualityRepairError("quality-repair-source-recovery-link-mismatch")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    if path.is_symlink() or not path.is_file():
        raise QualityRepairError("quality-repair-source-recovery-not-regular")
    destination_stat = os.lstat(path)
    if (
        not os.path.isfile(path)
        or destination_stat.st_dev != temporary_stat.st_dev
        or destination_stat.st_ino != temporary_stat.st_ino
        or destination_stat.st_nlink != 1
    ):
        raise QualityRepairError("quality-repair-source-recovery-unlink-mismatch")
    stored = _regular_json(path)
    if (
        stored != value
        or not isinstance(stored.get("receipt_sha256"), str)
        or stored["receipt_sha256"] != _receipt_hash(stored)
    ):
        raise QualityRepairError("quality-repair-source-recovery-content-mismatch")
    return _sha256(path)


def _atomic_create_text_once(path: Path, text: str) -> str:
    """Persist a new prompt exactly once without replacing any existing path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    expected = text.encode("utf-8")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise QualityRepairError("quality-repair-owner-prompt-exists") from exc
        temporary_stat = os.lstat(temporary)
        destination_stat = os.lstat(path)
        if not (
            os.path.isfile(temporary)
            and os.path.isfile(path)
            and temporary_stat.st_dev == destination_stat.st_dev
            and temporary_stat.st_ino == destination_stat.st_ino
            and temporary_stat.st_nlink == destination_stat.st_nlink == 2
        ):
            raise QualityRepairError("quality-repair-owner-prompt-link-mismatch")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    destination_stat = os.lstat(path)
    if (
        path.is_symlink()
        or not path.is_file()
        or destination_stat.st_dev != temporary_stat.st_dev
        or destination_stat.st_ino != temporary_stat.st_ino
        or destination_stat.st_nlink != 1
        or path.read_bytes() != expected
    ):
        raise QualityRepairError("quality-repair-owner-prompt-write-mismatch")
    return _sha256(path)


def _refused(reason: str) -> dict[str, str]:
    return {"status": "REFUSED", "reason": reason}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _owner_recovery_checkpoint(_phase: str) -> None:
    """Test seam for the durable prompt/receipt/state recovery boundaries."""


def _owner_is_alive(owner_pid: Any) -> bool:
    if not isinstance(owner_pid, int) or owner_pid <= 0:
        return False
    try:
        os.kill(owner_pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _age_seconds(timestamp: Any) -> float | None:
    if not isinstance(timestamp, str):
        return None
    try:
        started = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if started.tzinfo is None:
        return None
    return (datetime.now(timezone.utc) - started).total_seconds()


def _is_quality_block_audit(value: dict[str, Any]) -> bool:
    state = value.get("state")
    legacy = bool(
        value.get("platform") == "quality"
        and value.get("lang") == "ja+en"
        and value.get("published") is False
        and value.get("verified_logged_in") is False
        and value.get("draft_url") is None
        and value.get("live_url") is None
        and value.get("reality_gate") in {None, ""}
        and isinstance(state, str)
        and state.startswith("carry-over:quality-block:")
    )
    current = bool(
        value.get("platform") is None
        and value.get("lang") in {"ja", "en"}
        and value.get("published") is False
        and value.get("verified_logged_in") is False
        and value.get("draft_url") is None
        and value.get("live_url") is None
        and state == "quality-blocked:block_freeze"
        and isinstance(value.get("topic_id"), str)
        and value.get("topic_id", "").strip()
        and value.get("topic_source") == "paid-demand"
        and isinstance(value.get("editorial_form"), str)
        and value.get("editorial_form", "").strip()
    )
    return legacy or current


def _quality_audit_topic_id(ledger: Path, run_id: str) -> str | None:
    if not ledger.exists():
        return None
    rows: list[dict[str, Any]] = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict) and value.get("run_id") == run_id:
            rows.append(value)
    if (
        len(rows) != 2
        or {row.get("lang") for row in rows} != {"ja", "en"}
        or not all(_is_quality_block_audit(row) for row in rows)
    ):
        return None
    topics = {row.get("topic_id") for row in rows}
    return next(iter(topics)) if len(topics) == 1 else None


def _terminal_quality_block(
    run_dir: Path, ledger: Path
) -> dict[str, Any] | None:
    """Build a hash-bound rejection only for the known exhausted gate shape."""
    gates = run_dir / "gates"
    if (gates / "publication-state.json").exists() or ledger_has_public_effect(
        ledger, run_dir.name
    ):
        return None
    quality_path = gates / "quality-self-heal.json"
    blocker_path = gates / "quality-repair-blocker.json"
    quality = _read_json(quality_path)
    blocker = _read_json(blocker_path)
    route = _read_json(gates / "topic-route.json")
    if not (
        quality
        and quality.get("version") == CURRENT_QUALITY_VERSION
        and quality.get("attempt") == 2
        and quality.get("action") == "evaluate_reroute"
        and blocker
        and blocker.get("status") == "blocked"
        and blocker.get("run_id") == run_dir.name
        and blocker.get("action_from_quality_self_heal") == "evaluate_reroute"
        and blocker.get("publication") == "not_started"
        and blocker.get("archived_evidence") == "preserved"
        and "high-escalation-exhausted" in str(blocker.get("reason", ""))
        and route
        and isinstance(route.get("topic_id"), str)
        and route.get("topic_id", "").strip()
        and isinstance(route.get("editorial_form"), str)
        and route.get("editorial_form", "").strip()
    ):
        return None
    records = quality.get("quality")
    blocker_drafts = blocker.get("drafts")
    if not isinstance(records, dict) or not isinstance(blocker_drafts, dict):
        return None
    drafts: dict[str, str] = {}
    editorial_receipts: dict[str, str] = {}
    reader_receipts: dict[str, str] = {}
    feedback_items: list[dict[str, str]] = []
    reader_cap_languages: list[str] = []
    for lang in ("ja", "en"):
        article = run_dir / f"article-{lang}.md"
        editorial_path = gates / f"editorial-{lang}.json"
        reader_path = gates / f"reader-testing-gate-{lang}.terminal.json"
        attempt_path = gates / ".attempts" / f"reader-testing-gate-{lang}.json"
        editorial = _read_json(editorial_path)
        reader = _read_json(reader_path)
        attempt = _read_json(attempt_path)
        if not article.is_file() or article.is_symlink() or not editorial or not reader or not attempt:
            return None
        digest = _sha256(article)
        record = records.get(lang)
        if not (
            isinstance(record, dict)
            and record.get("article_sha256") == digest
            and blocker_drafts.get(lang) == digest
            and record.get("identity_current") is True
            and record.get("reader_current") is True
            and record.get("evaluation_current") is False
            and record.get("ready") is False
            and editorial.get("verdict") == "FAIL"
            and editorial.get("requested_reasoning_effort") == "high"
            and editorial.get("article_sha256") != digest
            and reader.get("article_sha256") == digest
            and attempt.get("article_sha256") == digest
        ):
            return None
        drafts[lang] = digest
        editorial_receipts[lang] = _sha256(editorial_path)
        reader_receipts[lang] = _sha256(reader_path)
        for index, fix in enumerate(editorial.get("fixes", []), start=1):
            if isinstance(fix, str) and fix.strip():
                feedback_items.append({"id": f"editorial-{lang}-{index}", "lang": lang, "kind": "editorial_fix", "text": fix.strip()})
        payload = reader.get("payload")
        if (
            int(attempt.get("attempts", 0)) >= 3
            and isinstance(payload, dict)
            and payload.get("verdict") == "FAIL"
            and payload.get("unanswered_questions")
        ):
            reader_cap_languages.append(lang)
    if not reader_cap_languages:
        return None
    return {
        "version": 1,
        "status": "terminal_quality_blocked",
        "run_id": run_dir.name,
        "reason": "evaluation_budget_exhausted",
        "created_at": _utc_now(),
        "drafts": drafts,
        "quality_sha256": _sha256(quality_path),
        "blocker_sha256": _sha256(blocker_path),
        "editorial_receipts": editorial_receipts,
        "reader_receipts": reader_receipts,
        "reader_cap_languages": reader_cap_languages,
        "topic_id": route["topic_id"].strip(),
        "editorial_form": route["editorial_form"].strip(),
        "quality_failure_feedback": {"version": 1, "source_run_id": run_dir.name, "items": feedback_items},
        "publication": "not_started",
    }


def _quality_module() -> Any:
    scripts = Path(__file__).resolve().parent
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import quality_self_heal  # pylint: disable=import-outside-toplevel

    return quality_self_heal


def _tracked_bookmark_source_defect(gates: Path) -> bool:
    defect = _read_json(gates / "source-defect-bookmark-ja.json")
    bookmark = _read_json(gates / "bookmark-ja.json")
    return bool(
        defect
        and defect.get("gate") == "bookmark"
        and defect.get("affected_artifact") == "article-ja"
        and defect.get("status") == "pending"
        and defect.get("verdict") == "FAIL"
        and defect.get("observed_output") == "FAIL names<2"
        and defect.get("source_file")
        == "skills/writer-agent/scripts/bookmark-gate.sh"
        and defect.get("source_immutable") is True
        and bookmark
        and bookmark.get("gate") in {"", "bookmark"}
        and bookmark.get("language") in {"", "ja"}
        and bookmark.get("verdict") == "FAIL"
        and bookmark.get("exit_code") == 1
        and bookmark.get("stdout") == "FAIL names<2\n"
    )


def _tracked_reader_terminal_source_defect(
    run_dir: Path, gates: Path, canonical: dict[str, Any]
) -> bool:
    defect = _read_json(gates / "quality-gate-defect.json")
    reader_source = Path(__file__).resolve().parent / "reader-testing-gate.sh"
    try:
        source_fixed = "READER_STDOUT_SCHEMA_VERSION=2" in reader_source.read_text(
            encoding="utf-8"
        )
    except OSError:
        return False
    if not (
        defect
        and defect.get("status") == "pending-source-fix"
        and defect.get("run_id") == run_dir.name
        and defect.get("component")
        == "reader-testing-gate.sh / quality_self_heal.py contract"
        and canonical.get("version") == CURRENT_QUALITY_VERSION
        and canonical.get("action") == "evaluate_reroute"
        and source_fixed
    ):
        return False
    quality = canonical.get("quality")
    if not isinstance(quality, dict):
        return False
    for lang in ("ja", "en"):
        article = run_dir / f"article-{lang}.md"
        attempt = _read_json(
            gates / ".attempts" / f"reader-testing-gate-{lang}.json"
        )
        terminal = _read_json(gates / f"reader-testing-gate-{lang}.terminal.json")
        if not article.is_file() or article.is_symlink():
            return False
        digest = _sha256(article)
        if (
            quality.get(lang, {}).get("article_sha256") != digest
            or not attempt
            or attempt.get("gate") != "reader-testing-gate"
            or attempt.get("lang") != lang
            or int(attempt.get("attempts", 0)) < 1
            or attempt.get("article_sha256") != digest
            or not terminal
            or terminal.get("verdict") not in {"PASS", "FAIL"}
            or terminal.get("article_sha256") is not None
        ):
            return False
    return True


def _quality_traceback(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8").rstrip("\n")
    except OSError:
        return False
    if not text.startswith("Traceback (most recent call last):"):
        return False
    if not (
        text.endswith(READER_TRACEBACK_SUFFIX)
        and text.splitlines()[-1]
        in {READER_TRACEBACK_SUFFIX, f"quality_self_heal.{READER_TRACEBACK_SUFFIX}"}
    ):
        return False
    return True


def _tracked_reader_terminal_receipt_source_defect(gates: Path) -> bool:
    terminal = gates / "reader-testing-gate-ja.terminal.json"
    return _quality_traceback(gates / "quality-self-heal.json") and not (
        terminal.exists() or terminal.is_symlink()
    )


def _qrr_lineage(
    run_dir: Path, ledger: Path, *, adopt: bool
) -> dict[str, Any] | None:
    prompt = run_dir / "article-daily-prompt.txt"
    generation = _regular_json(run_dir / "gates/generation-state.json")
    if (
        prompt.is_symlink()
        or not prompt.is_file()
        or not generation
        or generation.get("run_id") != run_dir.name
        or generation.get("run_dir") != str(run_dir)
        or generation.get("prompt_path") != str(prompt)
    ):
        return None
    try:
        prompt_sha256 = _sha256(prompt)
    except OSError:
        return None
    if generation.get("prompt_sha256") != prompt_sha256:
        return None
    status = generation.get("status")
    if status not in {"quality-repair-ready", "provider-returned"}:
        return None
    if status == "provider-returned":
        return {"status": status, "prompt": prompt}
    if adopt:
        try:
            result = generation_state.adopt_prepublication(
                run_dir, run_dir.name, prompt, ledger
            )
        except (generation_state.GenerationInvariant, OSError, ValueError, TypeError):
            return None
        if result != {"action": "unchanged", "status": "quality-repair-ready"}:
            return None
    gates = run_dir / "gates"
    receipt_path = gates / "prepublication-adoption.json"
    receipt = _read_json(receipt_path)
    if receipt_path.is_symlink() or not receipt:
        return None
    transitions = generation.get("transitions")
    transition = transitions[-1] if isinstance(transitions, list) and transitions else None
    if not isinstance(transition, dict) or transition.get("action") != "adopt-prepublication":
        return None
    if (
        receipt.get("receipt_sha256") != generation_state._adoption_receipt_hash(receipt)
        or transition.get("receipt_sha256") != receipt.get("receipt_sha256")
    ):
        return None
    try:
        receipt_file_sha256 = _sha256(receipt_path)
    except OSError:
        return None
    return {
        "status": status,
        "prompt": prompt,
        "qrr_lineage": {
            "adoption_receipt_sha256": receipt["receipt_sha256"],
            "adoption_receipt_file_sha256": receipt_file_sha256,
            "transition_receipt_sha256": transition["receipt_sha256"],
            "prompt_sha256": prompt_sha256,
        },
    }


def _tracked_editorial_hash_scope_source_defect(
    run_dir: Path, gates: Path, canonical: dict[str, Any]
) -> bool:
    blocker = _read_json(gates / "editorial-reroute-blocker.json")
    editorial_source = Path(__file__).resolve().parent / "editorial-gate.sh"
    try:
        source = editorial_source.read_text(encoding="utf-8")
    except OSError:
        return False
    if not (
        blocker
        and blocker.get("gate") == "editorial"
        and blocker.get("status") == "blocked"
        and blocker.get("exit_code") == 77
        and blocker.get("reason") == "high-escalation-exhausted"
        and canonical.get("version") == CURRENT_QUALITY_VERSION
        and canonical.get("attempt") == 2
        and canonical.get("action") == "evaluate_reroute"
        and 'HIGH_CLAIM_ROOT="$ARTICLE_RUN_DIR/gates/.attempts/editorial-high-$LANG_A"'
        in source
        and 'mkdir "$HIGH_CLAIM"' in source
    ):
        return False
    quality = canonical.get("quality")
    if not isinstance(quality, dict):
        return False
    for lang in ("ja", "en"):
        article = run_dir / f"article-{lang}.md"
        if not article.is_file() or article.is_symlink():
            return False
        digest = _sha256(article)
        if (
            quality.get(lang, {}).get("article_sha256") != digest
            or blocker.get(f"{lang}_article_sha256") != digest
        ):
            return False
    return True


def _editorial_source_has_active_repair_authorization() -> bool:
    source = Path(__file__).resolve().parent / "editorial-gate.sh"
    if source.is_symlink() or not source.is_file():
        return False
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return False
    return all(
        marker in text
        for marker in (
            "ARTICLE_QUALITY_REPAIR_ACTIVE",
            "ARTICLE_QUALITY_REPAIR_OWNER_PID",
            "quality-repair-state.json",
            'quality_action == "evaluate_reroute"',
            "owner_is_ancestor",
            'kill -0 "$ARTICLE_QUALITY_REPAIR_OWNER_PID"',
            'HIGH_CLAIM_ROOT="$ARTICLE_RUN_DIR/gates/.attempts/editorial-high-$LANG_A"',
            'mkdir "$HIGH_CLAIM"',
        )
    )


def _valid_reader_terminal(
    value: dict[str, Any], lang: str, article_hash: str
) -> bool:
    if (
        value.get("gate") != "reader-testing-gate"
        or value.get("lang") != lang
        or value.get("article_sha256") != article_hash
        or value.get("status") not in {"pass", "revision-required", "advisory"}
        or isinstance(value.get("attempts"), bool)
        or not isinstance(value.get("attempts"), int)
        or not 1 <= value["attempts"] <= 3
    ):
        return False
    status = value["status"]
    exit_code = value.get("exit_code")
    non_bool_exit = isinstance(exit_code, int) and not isinstance(exit_code, bool)
    if status == "pass":
        payload = value.get("payload")
        return (
            non_bool_exit
            and exit_code == 0
            and isinstance(payload, dict)
            and payload.get("verdict") == "PASS"
        )
    if status == "revision-required":
        return non_bool_exit and exit_code != 0
    if value["attempts"] != MAX_REPAIR_ATTEMPTS + 1:
        return False
    return (
        value.get("reason") == "max-attempts-reached"
        and "exit_code" not in value
    ) or (
        "reason" not in value and non_bool_exit and exit_code != 0
    )


def _tracked_active_editorial_repair_source_defect(
    run_dir: Path, gates: Path, repair_state: dict[str, Any]
) -> dict[str, Any] | None:
    state_path = gates / "quality-repair-state.json"
    if state_path.is_symlink() or not state_path.is_file():
        return None
    if not (
        repair_state.get("version") == 1
        and repair_state.get("status") == "terminal-incomplete"
        and repair_state.get("run_id") == run_dir.name
        and repair_state.get("repair_epoch") == REPAIR_EPOCH
        and isinstance(repair_state.get("attempts"), int)
        and not isinstance(repair_state.get("attempts"), bool)
        and repair_state.get("attempts") == MAX_REPAIR_ATTEMPTS
        and repair_state.get("quality_action") == "evaluate_reroute"
        and repair_state.get("source_defect") == "reader-terminal-receipt"
    ):
        return None
    recovery_path = gates / "quality-repair-source-recovery.json"
    if recovery_path.exists() or recovery_path.is_symlink():
        return None
    canonical_paths = (
        gates / "quality-self-heal.json",
        gates / "quality-self-heal-final.json",
        gates / "terminal-quality-blocked.json",
        gates / "publication-state.json",
    )
    if any(path.exists() or path.is_symlink() for path in canonical_paths):
        return None
    error_path = gates / "quality-self-heal-repair.err"
    if error_path.is_symlink() or not error_path.is_file():
        return None
    try:
        error_text = error_path.read_text(encoding="utf-8")
        error_sha256 = _sha256(error_path)
    except (OSError, UnicodeError):
        return None
    if error_text not in {
        SOURCE_RECOVERY_ERROR_SUFFIX,
        f"{SOURCE_RECOVERY_ERROR_SUFFIX}\n",
    } and not error_text.endswith(f"\n{SOURCE_RECOVERY_ERROR_SUFFIX}\n"):
        return None
    prompt_path = Path(str(repair_state.get("prompt_path", "")))
    expected_prompt_path = (
        run_dir
        / "gates"
        / "quality-repair"
        / f"epoch-{REPAIR_EPOCH}"
        / "repair-prompt.txt"
    )
    if (
        repair_state.get("prompt_path") != str(expected_prompt_path)
        or prompt_path != expected_prompt_path
        or expected_prompt_path.is_symlink()
        or not expected_prompt_path.is_file()
        or repair_state.get("prompt_sha256") != _sha256(expected_prompt_path)
    ):
        return None
    drafts: dict[str, str] = {}
    for lang in ("ja", "en"):
        article = run_dir / f"article-{lang}.md"
        if article.is_symlink() or not article.is_file():
            return None
        try:
            article_hash = _sha256(article)
        except OSError:
            return None
        drafts[lang] = article_hash
        identity = _regular_json(gates / f"identity-{lang}.json")
        editorial = _regular_json(gates / f"editorial-{lang}.json")
        reader = _regular_json(
            gates / f"reader-testing-gate-{lang}.terminal.json"
        )
        if not (
            identity
            and identity.get("verdict") == "PASS"
            and identity.get("article_sha256") == article_hash
            and editorial
            and editorial.get("verdict") == "FAIL"
            and editorial.get("requested_reasoning_effort") == "high"
            and isinstance(editorial.get("article_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", editorial["article_sha256"])
            and editorial["article_sha256"] != article_hash
            and reader
            and _valid_reader_terminal(reader, lang, article_hash)
        ):
            return None
    if not _editorial_source_has_active_repair_authorization():
        return None
    source = Path(__file__).resolve().parent / "editorial-gate.sh"
    try:
        editorial_source_sha256 = _sha256(source)
    except OSError:
        return None
    return {
        "drafts": drafts,
        "error_sha256": error_sha256,
        "editorial_source_sha256": editorial_source_sha256,
    }


def _tracked_repair_owner_prompt_source_defect(
    run_dir: Path, gates: Path, repair_state: dict[str, Any], *, allow_current_editorial: bool = False
) -> dict[str, Any] | None:
    if not (
        repair_state.get("version") == 1
        and repair_state.get("status") == "terminal-incomplete"
        and repair_state.get("run_id") == run_dir.name
        and repair_state.get("repair_epoch") == REPAIR_EPOCH
        and isinstance(repair_state.get("attempts"), int)
        and not isinstance(repair_state.get("attempts"), bool)
        and repair_state.get("attempts") == MAX_REPAIR_ATTEMPTS
        and repair_state.get("quality_action") == "evaluate_reroute"
        and repair_state.get("source_defect") == "reader-terminal-receipt"
    ):
        return None
    first_path = gates / "quality-repair-source-recovery.json"
    first = _regular_json(first_path)
    if not (
        first
        and first.get("schema") == "writer.quality-repair-source-recovery"
        and first.get("version") == 1
        and first.get("run_id") == run_dir.name
        and first.get("reason") == SOURCE_RECOVERY_REASON
        and first.get("recovery_attempt") == 1
        and isinstance(first.get("owner_pid"), int)
        and not isinstance(first.get("owner_pid"), bool)
        and first.get("receipt_sha256") == _receipt_hash(first)
        and repair_state.get("source_recovery_receipt_sha256") == _sha256(first_path)
        and repair_state.get("owner_pid") == first.get("owner_pid")
        and not _owner_is_alive(first.get("owner_pid"))
    ):
        return None
    owner_path = gates / "quality-repair-owner-recovery.json"
    owner_prompt = (
        gates
        / "quality-repair"
        / f"epoch-{REPAIR_EPOCH}"
        / "prompts"
        / "owner-recovery-1.txt"
    )
    if (
        owner_path.is_symlink()
        or owner_prompt.is_symlink()
        or (gates / "quality-self-heal.json").exists()
        or (gates / "quality-self-heal.json").is_symlink()
        or (gates / "quality-self-heal-final.json").exists()
        or (gates / "quality-self-heal-final.json").is_symlink()
        or (gates / "terminal-quality-blocked.json").exists()
        or (gates / "terminal-quality-blocked.json").is_symlink()
        or (gates / "publication-state.json").exists()
        or (gates / "publication-state.json").is_symlink()
    ):
        return None
    old_prompt = (
        gates / "quality-repair" / f"epoch-{REPAIR_EPOCH}" / "repair-prompt.txt"
    )
    if (
        old_prompt.is_symlink()
        or not old_prompt.is_file()
    ):
        return None
    try:
        old_prompt_text = old_prompt.read_text(encoding="utf-8")
    except OSError:
        return None
    if OWNER_PROMPT_PROHIBITION in old_prompt_text:
        return None
    expected_new_prompt = (
        old_prompt_text.rstrip("\n") + "\n\n" + OWNER_PROMPT_PROHIBITION + "\n"
    )
    prompt_exists = owner_prompt.exists()
    if prompt_exists and (
        not owner_prompt.is_file()
        or owner_prompt.read_bytes() != expected_new_prompt.encode("utf-8")
    ):
        return None
    owner_receipt = _regular_json(owner_path) if owner_path.exists() else None
    if owner_path.exists() and owner_receipt is None:
        return None
    drafts: dict[str, str] = {}
    for lang in ("ja", "en"):
        article = run_dir / f"article-{lang}.md"
        identity = _regular_json(gates / f"identity-{lang}.json")
        editorial = _regular_json(gates / f"editorial-{lang}.json")
        reader = _regular_json(gates / f"reader-testing-gate-{lang}.terminal.json")
        if article.is_symlink() or not article.is_file():
            return None
        try:
            article_hash = _sha256(article)
        except OSError:
            return None
        if not (
            identity
            and identity.get("verdict") == "PASS"
            and identity.get("article_sha256") == article_hash
            and editorial
            and editorial.get("verdict") == "FAIL"
            and editorial.get("requested_reasoning_effort") == "high"
            and isinstance(editorial.get("article_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", editorial["article_sha256"])
            and (allow_current_editorial or editorial["article_sha256"] != article_hash)
            and reader
            and _valid_reader_terminal(reader, lang, article_hash)
        ):
            return None
        drafts[lang] = article_hash
    if not _editorial_source_has_active_repair_authorization():
        return None
    quality_source = _quality_self_heal_orphan_occurrence_source()
    if quality_source is None:
        return None
    evidence = {
        "drafts": drafts,
        "first_recovery_sha256": _sha256(first_path),
        "old_prompt_path": str(old_prompt),
        "old_prompt_sha256": _sha256(old_prompt),
        "quality_self_heal_source_sha256": _sha256(quality_source),
        "new_prompt_path": str(owner_prompt),
        "new_prompt_text": expected_new_prompt,
    }
    state_is_old_prompt = (
        repair_state.get("prompt_path") == str(old_prompt)
        and repair_state.get("prompt_sha256") == evidence["old_prompt_sha256"]
    )
    if owner_receipt is None:
        if not state_is_old_prompt:
            return None
        evidence["phase"] = "prompt-created" if prompt_exists else "fresh"
        return evidence
    if not (
        owner_receipt.get("schema") == "writer.quality-repair-owner-recovery"
        and owner_receipt.get("version") == 1
        and owner_receipt.get("run_id") == run_dir.name
        and owner_receipt.get("reason") == OWNER_RECOVERY_REASON
        and owner_receipt.get("recovery_attempt") == 1
        and owner_receipt.get("source_recovery_receipt_sha256")
        == evidence["first_recovery_sha256"]
        and owner_receipt.get("old_prompt_path") == evidence["old_prompt_path"]
        and owner_receipt.get("old_prompt_sha256") == evidence["old_prompt_sha256"]
        and owner_receipt.get("new_prompt_path") == evidence["new_prompt_path"]
        and owner_receipt.get("new_prompt_sha256") == _sha256(owner_prompt)
        and owner_receipt.get("quality_self_heal_source_sha256")
        == evidence["quality_self_heal_source_sha256"]
        and owner_receipt.get("receipt_sha256") == _receipt_hash(owner_receipt)
    ):
        return None
    receipt_sha256 = _sha256(owner_path)
    if repair_state.get("owner_recovery_receipt_sha256") == receipt_sha256:
        if (
            repair_state.get("prompt_path") != str(owner_prompt)
            or repair_state.get("prompt_sha256") != _sha256(owner_prompt)
        ):
            return None
        evidence["phase"] = "state-bound"
        return evidence
    if (
        not state_is_old_prompt
        or owner_receipt.get("prior_state_sha256") != _sha256(gates / "quality-repair-state.json")
    ):
        return None
    evidence["phase"] = "receipt-created"
    evidence["owner_recovery_receipt_sha256"] = receipt_sha256
    return evidence


def _quality_self_heal_orphan_occurrence_source() -> Path | None:
    source = Path(__file__).resolve().parent / "quality_self_heal.py"
    if source.is_symlink() or not source.is_file():
        return None
    try:
        if _sha256(source) != QUALITY_SELF_HEAL_ORPHAN_OCCURRENCE_SHA256:
            return None
    except OSError:
        return None
    return source


def _orphaned_owner_prompt_recovery_evidence(
    run_dir: Path, gates: Path, state: dict[str, Any]
) -> bool:
    if not (
        state.get("status") == "invoking"
        and state.get("version") == 1
        and state.get("run_id") == run_dir.name
        and state.get("repair_epoch") == REPAIR_EPOCH
        and state.get("attempts") == MAX_REPAIR_ATTEMPTS
        and state.get("quality_action") == "evaluate_reroute"
        and state.get("source_defect") == "reader-terminal-receipt"
    ):
        return False
    first_path = gates / "quality-repair-source-recovery.json"
    owner_path = gates / "quality-repair-owner-recovery.json"
    first = _regular_json(first_path)
    owner = _regular_json(owner_path)
    prompt = gates / "quality-repair" / f"epoch-{REPAIR_EPOCH}" / "prompts" / "owner-recovery-1.txt"
    if not (first and owner and prompt.is_file() and not prompt.is_symlink()):
        return False
    validation_state = dict(state)
    validation_state["status"] = "terminal-incomplete"
    validation_state["owner_pid"] = first.get("owner_pid")
    full = _tracked_repair_owner_prompt_source_defect(
        run_dir, gates, validation_state, allow_current_editorial=True
    )
    if full is None or full.get("phase") != "state-bound":
        return False
    if not (
        first.get("schema") == "writer.quality-repair-source-recovery"
        and first.get("version") == 1 and first.get("run_id") == run_dir.name
        and first.get("reason") == SOURCE_RECOVERY_REASON and first.get("recovery_attempt") == 1
        and first.get("receipt_sha256") == _receipt_hash(first)
        and state.get("source_recovery_receipt_sha256") == _sha256(first_path)
        and owner.get("schema") == "writer.quality-repair-owner-recovery"
        and owner.get("version") == 1 and owner.get("run_id") == run_dir.name
        and owner.get("reason") == OWNER_RECOVERY_REASON and owner.get("recovery_attempt") == 1
        and owner.get("receipt_sha256") == _receipt_hash(owner)
        and owner.get("source_recovery_receipt_sha256") == _sha256(first_path)
        and owner.get("owner_pid") == state.get("owner_pid")
        and isinstance(owner.get("owner_pid"), int) and owner.get("owner_pid") > 0
        and state.get("owner_recovery_receipt_sha256") == _sha256(owner_path)
        and state.get("prompt_path") == str(prompt)
        and state.get("prompt_sha256") == _sha256(prompt)
        and owner.get("new_prompt_path") == str(prompt)
        and owner.get("new_prompt_sha256") == _sha256(prompt)
        and OWNER_PROMPT_PROHIBITION in prompt.read_text(encoding="utf-8")
    ):
        return False
    return _quality_self_heal_orphan_occurrence_source() is not None


def _tracked_topic_router_reroute_source_defect(
    run_dir: Path, gates: Path, canonical: dict[str, Any]
) -> bool:
    router_source = Path(__file__).resolve().parent / "topic_router.py"
    try:
        source = router_source.read_text(encoding="utf-8")
    except OSError:
        return False
    attempt_one = _read_json(gates / "quality-self-heal-attempt-1.json")
    attempt_two = _read_json(gates / "quality-self-heal-attempt-2.json")
    route = _read_json(gates / "topic-route.json")
    generation = _read_json(gates / "generation-state.json")
    attempts = generation.get("attempts") if generation else None
    if not (
        "current-run reroute must preserve topic_id" in source
        and "current-run reroute must change editorial_form" in source
        and canonical.get("version") == CURRENT_QUALITY_VERSION
        and canonical.get("attempt") == 2
        and canonical.get("action") == "block_freeze"
        and canonical.get("reason") == "editorial_form_not_changed"
        and attempt_two == canonical
        and attempt_one
        and attempt_one.get("version") == CURRENT_QUALITY_VERSION
        and attempt_one.get("attempt") == 1
        and attempt_one.get("action") == "reroute"
        and attempt_one.get("required_changes") == ["editorial_form", "outline"]
        and isinstance(attempt_one.get("forbidden_editorial_form"), str)
        and attempt_one.get("forbidden_editorial_form")
        == attempt_one.get("editorial_form")
        and route
        and isinstance(route.get("topic_id"), str)
        and route.get("topic_id", "").strip()
        and route.get("editorial_form")
        == attempt_one.get("forbidden_editorial_form")
        and isinstance(attempts, list)
        and len(attempts) == 2
        and all(
            isinstance(row, dict)
            and row.get("attempt") == index
            and row.get("status") == "provider-returned"
            and row.get("return_code") == 0
            for index, row in enumerate(attempts, start=1)
        )
    ):
        return False
    quality = canonical.get("quality")
    attempt_quality = attempt_one.get("quality")
    if not isinstance(quality, dict) or not isinstance(attempt_quality, dict):
        return False
    for lang in ("ja", "en"):
        article = run_dir / f"article-{lang}.md"
        if not article.is_file() or article.is_symlink():
            return False
        digest = _sha256(article)
        prior_digest = attempt_quality.get(lang, {}).get("article_sha256")
        if (
            quality.get(lang, {}).get("article_sha256") != digest
            or not isinstance(prior_digest, str)
            or len(prior_digest) != 64
            or prior_digest == digest
        ):
            return False
    return True


def _bookmark_gate_now_passes(run_dir: Path, article: Path) -> bool:
    try:
        title = next(
            line[2:].strip()
            for line in article.read_text(encoding="utf-8").splitlines()
            if line.startswith("# ") and line[2:].strip()
        )
    except (OSError, StopIteration):
        return False
    gate = Path(__file__).resolve().parent / "bookmark-gate.sh"
    try:
        completed = subprocess.run(
            [
                "bash",
                str(gate),
                title,
                str(article),
                "--lang",
                "ja",
            ],
            cwd=run_dir,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and completed.stdout.startswith("OK ")


def plan(run_dir: Path | str, ledger: Path | str) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    ledger = Path(ledger).resolve()
    gates = run_dir / "gates"
    run_id = run_dir.name
    if not run_dir.is_dir() or not gates.is_dir():
        return _refused("run-directory-missing")
    if (gates / "publication-state.json").exists():
        return _refused("publication-state-exists")
    if ledger_has_public_effect(ledger, run_id):
        return _refused("ledger-row-exists")
    repair_state = _regular_json(gates / "quality-repair-state.json")
    if repair_state:
        lineage = _qrr_lineage(run_dir, ledger, adopt=False)
        if lineage is None:
            return _refused(QUALITY_REPAIR_EVIDENCE_INVALID)
        generation_status = lineage["status"]
        qrr_lineage = lineage.get("qrr_lineage")
        if generation_status == "quality-repair-ready":
            if repair_state.get("qrr_lineage") != qrr_lineage:
                return _refused(QUALITY_REPAIR_EVIDENCE_INVALID)
        elif repair_state.get("qrr_lineage") is not None:
            return _refused(QUALITY_REPAIR_EVIDENCE_INVALID)
        prompt_path = Path(str(repair_state.get("prompt_path", "")))
        if (
            prompt_path.is_symlink()
            or not prompt_path.is_file()
            or repair_state.get("prompt_sha256") != _sha256(prompt_path)
        ):
            return _refused(QUALITY_REPAIR_EVIDENCE_INVALID)
        try:
            attempts = int(repair_state.get("attempts", 0))
        except (TypeError, ValueError):
            return _refused(QUALITY_REPAIR_EVIDENCE_INVALID)
        if repair_state.get("status") == "terminal-incomplete":
            owner_recovery_path = gates / "quality-repair-owner-recovery.json"
            owner_recovery_evidence = _tracked_repair_owner_prompt_source_defect(
                run_dir, gates, repair_state
            )
            if owner_recovery_evidence is not None:
                return {
                    "status": "READY",
                    "reason": OWNER_RECOVERY_REASON,
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "repair_epoch": REPAIR_EPOCH,
                    "attempts": attempts,
                    "prompt_path": str(prompt_path),
                    "prompt_sha256": repair_state["prompt_sha256"],
                }
            if owner_recovery_path.exists() or owner_recovery_path.is_symlink():
                return _refused("quality-repair-owner-recovery-already-recorded")
            recovery_path = gates / "quality-repair-source-recovery.json"
            if (
                recovery_path.exists()
                or recovery_path.is_symlink()
                or "source_recovery_receipt_sha256" in repair_state
            ):
                return _refused("quality-repair-source-recovery-already-recorded")
            recovery_evidence = _tracked_active_editorial_repair_source_defect(
                run_dir, gates, repair_state
            )
            if recovery_evidence is not None:
                return {"status":"READY","reason":SOURCE_RECOVERY_REASON,"run_id":run_id,"run_dir":str(run_dir),"repair_epoch":REPAIR_EPOCH,"attempts":attempts,"prompt_path":str(prompt_path),"prompt_sha256":repair_state["prompt_sha256"]}
        if repair_state.get("status") == "invoking":
            age = _age_seconds(repair_state.get("started_at"))
            if (
                _orphaned_owner_prompt_recovery_evidence(run_dir, gates, repair_state)
                and not _owner_is_alive(repair_state.get("owner_pid"))
                and age is not None
                and age >= 60
            ):
                return {
                    "status": "READY",
                    "reason": "orphaned-owner-prompt-recovery",
                    "run_id": run_id,
                    "run_dir": str(run_dir),
                    "repair_epoch": REPAIR_EPOCH,
                    "attempts": attempts,
                    "prompt_path": repair_state["prompt_path"],
                    "prompt_sha256": repair_state["prompt_sha256"],
                    "orphaned_owner_pid": repair_state.get("owner_pid"),
                }
        if _terminal_quality_block(run_dir, ledger) is not None:
            return {
                "status": "READY",
                "reason": "structurally-exhausted-quality-evaluations",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "repair_epoch": REPAIR_EPOCH,
                "attempts": attempts,
            }
        if (
            repair_state.get("status")
            in {"prepared", "retryable-incomplete"}
            and attempts < MAX_REPAIR_ATTEMPTS
        ):
            return {
                "status": "READY",
                "reason": "prepared-quality-repair",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "repair_epoch": REPAIR_EPOCH,
                "prompt_path": str(prompt_path),
                "prompt_sha256": repair_state["prompt_sha256"],
                "attempts": attempts,
            }
        age = _age_seconds(repair_state.get("started_at"))
        if (
            repair_state.get("status") == "invoking"
            and attempts < MAX_REPAIR_ATTEMPTS
            and not _owner_is_alive(repair_state.get("owner_pid"))
            and age is not None
            and age >= 60
        ):
            return {
                "status": "READY",
                "reason": "orphaned-quality-repair",
                "run_id": run_id,
                "run_dir": str(run_dir),
                "repair_epoch": REPAIR_EPOCH,
                "prompt_path": str(prompt_path),
                "prompt_sha256": repair_state["prompt_sha256"],
                "attempts": attempts,
                "orphaned_owner_pid": repair_state.get("owner_pid"),
            }
        return _refused(
            f"quality-repair-already-{repair_state.get('status', 'unknown')}"
        )
    lineage = _qrr_lineage(run_dir, ledger, adopt=True)
    if lineage is None:
        return _refused("generation-state-not-provider-returned")
    generation_status = lineage["status"]
    qrr_lineage = lineage.get("qrr_lineage")
    prompt = lineage["prompt"]
    canonical = _read_json(gates / "quality-self-heal.json")
    legacy = bool(
        canonical
        and canonical.get("version") == 1
        and canonical.get("action") == "block_freeze"
    )
    source_defect = bool(
        canonical
        and canonical.get("version") == CURRENT_QUALITY_VERSION
        and canonical.get("action") == "block_freeze"
        and _tracked_bookmark_source_defect(gates)
    )
    reader_source_defect = bool(
        canonical
        and _tracked_reader_terminal_source_defect(run_dir, gates, canonical)
    )
    reader_receipt_source_defect = (
        _tracked_reader_terminal_receipt_source_defect(gates)
        if generation_status == "quality-repair-ready"
        else None
    )
    editorial_source_defect = bool(
        canonical
        and _tracked_editorial_hash_scope_source_defect(
            run_dir, gates, canonical
        )
    )
    topic_router_source_defect = bool(
        canonical
        and _tracked_topic_router_reroute_source_defect(
            run_dir, gates, canonical
        )
    )
    if (
        not legacy
        and not source_defect
        and not reader_source_defect
        and not reader_receipt_source_defect
        and not editorial_source_defect
        and not topic_router_source_defect
    ):
        return _refused("quality-block-not-legacy")
    if topic_router_source_defect:
        return {
            "status": "READY",
            "reason": "tracked-topic-router-reroute-source-defect",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "repair_epoch": REPAIR_EPOCH,
            "source_defect": "topic-router-reroute",
            "drafts": {
                lang: _sha256(run_dir / f"article-{lang}.md")
                for lang in ("ja", "en")
            },
        }
    if editorial_source_defect:
        return {
            "status": "READY",
            "reason": "tracked-editorial-hash-scope-source-defect",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "repair_epoch": REPAIR_EPOCH,
            "source_defect": "editorial-hash-scope",
            "drafts": {
                lang: _sha256(run_dir / f"article-{lang}.md")
                for lang in ("ja", "en")
            },
        }
    if reader_source_defect:
        return {
            "status": "READY",
            "reason": "tracked-reader-terminal-source-defect",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "repair_epoch": REPAIR_EPOCH,
            "source_defect": "reader-terminal-hash",
            "drafts": {
                lang: _sha256(run_dir / f"article-{lang}.md")
                for lang in ("ja", "en")
            },
        }
    if reader_receipt_source_defect:
        return {
            "status": "READY",
            "reason": "tracked-reader-terminal-receipt-source-defect",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "repair_epoch": REPAIR_EPOCH,
            "source_defect": "reader-terminal-receipt",
            "quality_traceback_sha256": _sha256(
                gates / "quality-self-heal.json"
            ),
            "reader_terminal": "gates/reader-testing-gate-ja.terminal.json",
            "qrr_lineage": qrr_lineage,
            "drafts": {
                lang: _sha256(run_dir / f"article-{lang}.md")
                for lang in ("ja", "en")
            },
        }
    attempt_one = _read_json(gates / "quality-self-heal-attempt-1.json")
    attempt_two = _read_json(gates / "quality-self-heal-attempt-2.json")
    expected_version = CURRENT_QUALITY_VERSION if source_defect else 1
    if (
        not attempt_one
        or attempt_one.get("version") != expected_version
        or attempt_one.get("action") != "reroute"
        or not attempt_two
        or attempt_two != canonical
    ):
        return _refused("legacy-quality-history-invalid")
    route = _read_json(gates / "topic-route.json")
    forbidden = attempt_one.get("forbidden_editorial_form")
    if (
        not route
        or not isinstance(forbidden, str)
        or route.get("editorial_form") == forbidden
    ):
        return _refused("legacy-reroute-not-distinct")
    drafts = {
        "ja": run_dir / "article-ja.md",
        "en": run_dir / "article-en.md",
    }
    if any(
        not path.is_file() or path.is_symlink() for path in drafts.values()
    ):
        return _refused("draft-missing-or-unsafe")
    canonical_quality = canonical.get("quality")
    if not isinstance(canonical_quality, dict) or any(
        not isinstance(canonical_quality.get(lang), dict)
        or canonical_quality[lang].get("article_sha256")
        != _sha256(drafts[lang])
        for lang in ("ja", "en")
    ):
        return _refused("legacy-block-draft-hash-mismatch")
    if source_defect:
        if not _bookmark_gate_now_passes(run_dir, drafts["ja"]):
            return _refused("tracked-bookmark-source-defect-still-failing")
        return {
            "status": "READY",
            "reason": "tracked-bookmark-source-defect",
            "run_id": run_id,
            "run_dir": str(run_dir),
            "repair_epoch": REPAIR_EPOCH,
            "source_defect": "bookmark-ja",
            "drafts": {
                lang: _sha256(path) for lang, path in drafts.items()
            },
        }
    quality = _quality_module()
    current = {
        lang: quality.language_quality(run_dir, lang, drafts[lang])
        for lang in ("ja", "en")
    }
    if all(row.get("evaluation_current") is True for row in current.values()):
        return _refused("legacy-block-evaluations-already-current")
    return {
        "status": "READY",
        "reason": "legacy-stale-quality-block",
        "run_id": run_id,
        "run_dir": str(run_dir),
        "repair_epoch": REPAIR_EPOCH,
        "drafts": {lang: _sha256(path) for lang, path in drafts.items()},
    }


def _active_receipts(gates: Path) -> list[Path]:
    names = [
        "attempt-budget.json",
        "editorial-ja.json",
        "editorial-en.json",
        "identity-ja.json",
        "identity-en.json",
        "quality-self-heal-attempt-2.json",
        "quality-self-heal.json",
        "reader-testing-gate-ja.terminal.json",
        "reader-testing-gate-en.terminal.json",
        "bookmark-ja.json",
        "source-defect-bookmark-ja.json",
        "quality-gate-defect.json",
        "editorial-reroute-blocker.json",
        "quality-self-heal-final.json",
    ]
    paths = [gates / name for name in names if (gates / name).is_file()]
    attempts = gates / ".attempts"
    if attempts.is_dir():
        paths.extend(
            path
            for path in sorted(attempts.rglob("*"))
            if path.is_file() and not path.is_symlink()
        )
    return paths


def _repair_prompt(
    run_dir: Path, ledger: Path, archive: Path, source_defect: str | None
) -> str:
    scripts = Path(__file__).resolve().parent
    if source_defect == "topic-router-reroute":
        return f"""Run ONE bounded reroute repair for the existing unpublished Writer run.

RUN_DIR={run_dir}
LEDGER={ledger}
ORIGINAL_PROMPT={run_dir / "article-daily-prompt.txt"}
ARCHIVED_EVIDENCE={archive}

The canonical quality receipt is the restored attempt-1 action=reroute. Read it and gates/topic-route.json. Run topic_router.py validate through the normal route command, preserve the current topic_id exactly, and select an editorial_form different from forbidden_editorial_form. Do not hand-edit topic-route.json. Rewrite the outline plus article-ja.md and article-en.md for that same topic in the new form.

No staging or publication is allowed until every current-hash editorial, identity, reader, media, CTA, and quality gate passes and quality_self_heal.py returns action=ready_to_freeze. The archived receipts are immutable. If any gate returns block_freeze, preserve evidence, publish nothing, and stop. On ready_to_freeze, continue only STEP 4.8 through STEP 20 of ORIGINAL_PROMPT for this same run, with real remote readback and no local-only publication claim.
"""
    if source_defect in {"reader-terminal-hash", "reader-terminal-receipt"}:
        first_gate = (
            "Run reader-testing-gate.sh for BOTH languages first and verify "
            "each terminal carries the current article SHA-256."
        )
    elif source_defect == "editorial-hash-scope":
        first_gate = (
            "Run editorial-gate.sh for BOTH languages first and verify each "
            "receipt carries the current article SHA-256."
        )
    else:
        first_gate = (
            "Run bookmark-gate.sh for JA first; if it does not PASS on the "
            "current bytes, publish nothing and stop."
        )
    return f"""Run ONE bounded quality repair for the existing unpublished Writer run.

RUN_DIR={run_dir}
LEDGER={ledger}
ORIGINAL_PROMPT={run_dir / "article-daily-prompt.txt"}
ARCHIVED_EVIDENCE={archive}

Hard boundaries:
- This model call is the active repair executor. The wrapper sets ARTICLE_QUALITY_REPAIR_ACTIVE=1 and ARTICLE_QUALITY_REPAIR_OWNER_PID to its own parent PID.
- {OWNER_PROMPT_PROHIBITION}
- quality-repair-state status=invoking and that owner PID describe this exact execution. Do not wait for quality-repair-state or monitor its owner; doing so deadlocks the child against its parent. Start the gate work immediately.
- Do not select a new topic, perform new research, create another run, or change gates/topic-route.json.
- Do not replace the JA/EN artifact identities. Work only on article-ja.md and article-en.md in RUN_DIR.
- No staging or publication is allowed until quality_self_heal.py returns action=ready_to_freeze and current-hash quality terminals exist.
- The archived receipts under ARCHIVED_EVIDENCE are immutable evidence. Never edit, delete, or move them.

Read the archived receipts, editorial and reader findings, the current route, and both current drafts. {first_gate} Run the remaining current deterministic checks. For BOTH languages, run editorial-gate.sh, identity-gate.sh, and reader-testing-gate.sh against the current bytes. Apply only the bounded revisions those gates request; after any revision, restore and validate canonical media and CTA invariants before re-running the relevant gate. A same-hash editorial FAIL must be revised before rejudge. Reader questions remain stable and each language gets at most three evaluations.

Then run:
python3 {scripts / "quality_self_heal.py"} assess --run-dir "$ARTICLE_RUN_DIR" --draft-ja "$ARTICLE_RUN_DIR/article-ja.md" --draft-en "$ARTICLE_RUN_DIR/article-en.md"

If action=evaluate_reroute, run every missing current-hash editorial, identity, and reader evaluation before calling assess again. If action=block_freeze, preserve the evidence, publish nothing, report the honest block, and stop. If action=ready_to_freeze, read ORIGINAL_PROMPT and continue only STEP 4.8 through STEP 20 for this same run. The original armed money contract remains authoritative: note is ¥500 and both Substack posts are paid-only. Require real remote readback and never claim publication from a local receipt.
"""


def begin(run_dir: Path | str, ledger: Path | str) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    ledger = Path(ledger).resolve()
    decision = plan(run_dir, ledger)
    if decision.get("status") != "READY":
        raise QualityRepairError(str(decision.get("reason", "not-ready")))
    gates = run_dir / "gates"
    archive_parent = gates / "quality-repair" / f"epoch-{REPAIR_EPOCH}"
    archive = archive_parent / "original"
    if archive_parent.exists():
        raise QualityRepairError("quality-repair-archive-exists")
    active = _active_receipts(gates)
    entries: list[dict[str, str]] = []
    for source in active:
        relative = source.relative_to(gates)
        entries.append(
            {
                "path": str(relative),
                "sha256": _sha256(source),
            }
        )
    state = {
        "version": 1,
        "status": "archiving",
        "run_id": run_dir.name,
        "repair_epoch": REPAIR_EPOCH,
        "attempts": 0,
        "drafts": decision["drafts"],
        "source_defect": decision.get("source_defect"),
        "route_sha256": _sha256(gates / "topic-route.json"),
        "topic_id": _read_json(gates / "topic-route.json").get("topic_id"),
        "entries": entries,
    }
    if decision.get("qrr_lineage") is not None:
        state["qrr_lineage"] = decision["qrr_lineage"]
    state_path = gates / "quality-repair-state.json"
    _atomic_write(state_path, state)
    archive.mkdir(parents=True)
    for row, source in zip(entries, active, strict=True):
        destination = archive / row["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        if (
            not destination.is_file()
            or destination.is_symlink()
            or _sha256(destination) != row["sha256"]
        ):
            raise QualityRepairError("quality-repair-archive-verification-failed")
    attempts = gates / ".attempts"
    if attempts.exists():
        shutil.rmtree(attempts)
    _atomic_write(
        archive_parent / "manifest.json",
        {
            "version": 1,
            "run_id": run_dir.name,
            "repair_epoch": REPAIR_EPOCH,
            "entries": entries,
        },
    )
    if decision.get("source_defect") == "topic-router-reroute":
        current = _read_json(gates / "quality-self-heal-attempt-1.json")
        if not current:
            raise QualityRepairError("quality-reroute-attempt-one-missing")
        _atomic_write(gates / "quality-self-heal.json", current)
    elif decision.get("source_defect") == "reader-terminal-receipt":
        source_receipt = archive / "quality-self-heal.json"
        if not source_receipt.is_file() or source_receipt.is_symlink():
            raise QualityRepairError("reader-quality-traceback-not-archived")
        current = {
            "version": CURRENT_QUALITY_VERSION,
            "action": "evaluate_reroute",
        }
    else:
        quality = _quality_module()
        current = quality.assess(
            run_dir,
            {
                "ja": run_dir / "article-ja.md",
                "en": run_dir / "article-en.md",
            },
        )
    if (
        current.get("version") != CURRENT_QUALITY_VERSION
        or current.get("action")
        != (
            "reroute"
            if decision.get("source_defect") == "topic-router-reroute"
            else "evaluate_reroute"
        )
    ):
        raise QualityRepairError("quality-repair-did-not-open-current-evaluation")
    prompt_path = archive_parent / "repair-prompt.txt"
    prompt_path.write_text(
        _repair_prompt(run_dir, ledger, archive, decision.get("source_defect")),
        encoding="utf-8",
    )
    state.update(
        {
            "status": "prepared",
            "quality_action": current["action"],
            "prompt_path": str(prompt_path),
            "prompt_sha256": _sha256(prompt_path),
        }
    )
    _atomic_write(gates / "quality-repair-state.json", state)
    return state


def mark_invoking(
    run_dir: Path | str,
    ledger: Path | str,
    *,
    owner_pid: int,
) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    ledger = Path(ledger).resolve()
    decision = plan(run_dir, ledger)
    if (
        decision.get("status") != "READY"
        or decision.get("reason")
        not in {
            "prepared-quality-repair",
            "orphaned-quality-repair",
            SOURCE_RECOVERY_REASON,
            OWNER_RECOVERY_REASON,
            "orphaned-owner-prompt-recovery",
        }
    ):
        raise QualityRepairError(str(decision.get("reason", "not-prepared")))
    state_path = run_dir / "gates" / "quality-repair-state.json"
    state = _regular_json(state_path)
    if not state:
        raise QualityRepairError("quality-repair-state-missing")
    route = run_dir / "gates" / "topic-route.json"
    if state.get("route_sha256") != _sha256(route):
        current_route = _read_json(route)
        archived_attempt = _read_json(
            run_dir
            / "gates"
            / "quality-repair"
            / f"epoch-{REPAIR_EPOCH}"
            / "original"
            / "quality-self-heal.json"
        )
        expected_topic = state.get("topic_id") or _quality_audit_topic_id(
            ledger, run_dir.name
        )
        if not (
            state.get("source_defect") == "topic-router-reroute"
            and current_route
            and current_route.get("topic_id") == expected_topic
            and isinstance(current_route.get("editorial_form"), str)
            and current_route.get("editorial_form", "").strip()
            and archived_attempt
            and current_route.get("editorial_form")
            != archived_attempt.get("editorial_form")
        ):
            raise QualityRepairError("quality-repair-route-changed")
        state["topic_id"] = expected_topic
        state["route_sha256"] = _sha256(route)
    source_recovery = decision.get("reason") == SOURCE_RECOVERY_REASON
    owner_recovery = decision.get("reason") == OWNER_RECOVERY_REASON
    owner_orphan_recovery = decision.get("reason") == "orphaned-owner-prompt-recovery"
    attempts = int(state.get("attempts", 0))
    if not source_recovery and not owner_recovery and not owner_orphan_recovery:
        attempts += 1
        if attempts > MAX_REPAIR_ATTEMPTS:
            raise QualityRepairError("quality-repair-attempt-limit")
    if decision.get("reason") == "orphaned-quality-repair":
        old_prompt = Path(str(state.get("prompt_path", "")))
        old_hash = str(state.get("prompt_sha256", ""))
        prompts = (
            run_dir
            / "gates"
            / "quality-repair"
            / f"epoch-{REPAIR_EPOCH}"
            / "prompts"
        )
        prompts.mkdir(parents=True, exist_ok=True)
        archived_prompt = prompts / f"attempt-{attempts - 1}.txt"
        if archived_prompt.exists():
            raise QualityRepairError("quality-repair-prompt-archive-conflicts")
        shutil.copy2(old_prompt, archived_prompt)
        if _sha256(archived_prompt) != old_hash:
            raise QualityRepairError("quality-repair-prompt-archive-mismatch")
        new_prompt = (
            run_dir
            / "gates"
            / "quality-repair"
            / f"epoch-{REPAIR_EPOCH}"
            / f"repair-prompt-attempt-{attempts}.txt"
        )
        new_prompt.write_text(
                _repair_prompt(
                    run_dir,
                    ledger,
                run_dir
                / "gates"
                / "quality-repair"
                    / f"epoch-{REPAIR_EPOCH}"
                    / "original",
                    state.get("source_defect"),
                ),
            encoding="utf-8",
        )
        state.update(
            {
                "prompt_path": str(new_prompt),
                "prompt_sha256": _sha256(new_prompt),
                "orphaned_owner_pid": decision.get(
                    "orphaned_owner_pid"
                ),
            }
        )
    if source_recovery:
        recovery_evidence = _tracked_active_editorial_repair_source_defect(
            run_dir, run_dir / "gates", state
        )
        if recovery_evidence is None:
            raise QualityRepairError(QUALITY_REPAIR_EVIDENCE_INVALID)
        recovery_path = run_dir / "gates" / "quality-repair-source-recovery.json"
        if recovery_path.exists() or recovery_path.is_symlink():
            raise QualityRepairError("quality-repair-source-recovery-exists")
        prior_state_sha256 = _sha256(state_path)
        receipt = {
            "schema": "writer.quality-repair-source-recovery",
            "version": 1,
            "run_id": run_dir.name,
            "reason": SOURCE_RECOVERY_REASON,
            "recovery_attempt": 1,
            "prior_state_sha256": prior_state_sha256,
            "error_sha256": recovery_evidence["error_sha256"],
            "editorial_source_sha256": recovery_evidence[
                "editorial_source_sha256"
            ],
            "drafts": recovery_evidence["drafts"],
            "owner_pid": owner_pid,
            "created_at": _utc_now(),
        }
        receipt["receipt_sha256"] = _receipt_hash(receipt)
        state["source_recovery_receipt_sha256"] = _atomic_create_once(
            recovery_path, receipt
        )
        _atomic_write(state_path, state)
    if owner_recovery:
        recovery_evidence = _tracked_repair_owner_prompt_source_defect(
            run_dir, run_dir / "gates", state
        )
        if recovery_evidence is None:
            raise QualityRepairError(QUALITY_REPAIR_EVIDENCE_INVALID)
        prompts = (
            run_dir
            / "gates"
            / "quality-repair"
            / f"epoch-{REPAIR_EPOCH}"
            / "prompts"
        )
        new_prompt = prompts / "owner-recovery-1.txt"
        old_prompt = Path(recovery_evidence["old_prompt_path"])
        phase = recovery_evidence["phase"]
        if phase == "fresh":
            new_prompt_sha256 = _atomic_create_text_once(
                new_prompt, recovery_evidence["new_prompt_text"]
            )
            _owner_recovery_checkpoint("after-prompt-create")
        else:
            new_prompt_sha256 = _sha256(new_prompt)
        recovery_path = run_dir / "gates" / "quality-repair-owner-recovery.json"
        if phase in {"fresh", "prompt-created"}:
            receipt = {
                "schema": "writer.quality-repair-owner-recovery",
                "version": 1,
                "run_id": run_dir.name,
                "reason": OWNER_RECOVERY_REASON,
                "recovery_attempt": 1,
                "source_recovery_receipt_sha256": recovery_evidence[
                    "first_recovery_sha256"
                ],
                "prior_state_sha256": _sha256(state_path),
                "old_prompt_path": recovery_evidence["old_prompt_path"],
                "old_prompt_sha256": recovery_evidence["old_prompt_sha256"],
                "new_prompt_path": str(new_prompt),
                "new_prompt_sha256": new_prompt_sha256,
                "quality_self_heal_source_sha256": recovery_evidence[
                    "quality_self_heal_source_sha256"
                ],
                "owner_pid": owner_pid,
                "created_at": _utc_now(),
            }
            receipt["receipt_sha256"] = _receipt_hash(receipt)
            owner_receipt_sha256 = _atomic_create_once(recovery_path, receipt)
            _owner_recovery_checkpoint("after-receipt-create")
        else:
            owner_receipt_sha256 = _sha256(recovery_path)
        state.update(
            {
                "owner_recovery_receipt_sha256": owner_receipt_sha256,
                "prompt_path": str(new_prompt),
                "prompt_sha256": new_prompt_sha256,
            }
        )
        _atomic_write(state_path, state)
        _owner_recovery_checkpoint("after-state-bind")
    state.update(
        {
            "status": "invoking",
            "attempts": attempts,
            "owner_pid": owner_pid,
            "started_at": _utc_now(),
        }
    )
    _atomic_write(state_path, state)
    return state


def record_result(
    run_dir: Path | str,
    ledger: Path | str,
    *,
    return_code: int,
) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    ledger = Path(ledger).resolve()
    gates = run_dir / "gates"
    state_path = gates / "quality-repair-state.json"
    state = _read_json(state_path)
    if not state or state.get("status") != "invoking":
        raise QualityRepairError("quality-repair-not-invoking")
    attempts = int(state.get("attempts", 0))
    quality = _read_json(gates / "quality-self-heal.json")
    terminal = _terminal_quality_block(run_dir, ledger)
    if terminal is not None:
        _atomic_write(gates / "terminal-quality-blocked.json", terminal)
        status = "terminal-quality-blocked"
    elif (gates / "publication-state.json").is_file():
        status = "handed-to-publication"
    elif ledger_has_public_effect(ledger, run_dir.name):
        status = "terminal-ambiguous-ledger-without-publication-state"
    elif quality and quality.get("action") == "block_freeze":
        status = "terminal-blocked"
    elif attempts < MAX_REPAIR_ATTEMPTS:
        status = "retryable-incomplete"
    else:
        status = "terminal-incomplete"
    state.update(
        {
            "status": status,
            "return_code": return_code,
            "finished_at": _utc_now(),
        }
    )
    _atomic_write(state_path, state)
    return state


def terminalize(run_dir: Path | str, ledger: Path | str) -> dict[str, Any]:
    """Close an already-finished repair without another provider invocation."""
    run_dir = Path(run_dir).resolve()
    ledger = Path(ledger).resolve()
    gates = run_dir / "gates"
    terminal = _terminal_quality_block(run_dir, ledger)
    state_path = gates / "quality-repair-state.json"
    state = _read_json(state_path)
    if terminal is None or state is None:
        raise QualityRepairError("quality-evaluations-not-structurally-exhausted")
    _atomic_write(gates / "terminal-quality-blocked.json", terminal)
    state.update({
        "status": "terminal-quality-blocked",
        "finished_at": terminal["created_at"],
    })
    _atomic_write(state_path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("plan", "begin", "invoke", "result", "terminalize"),
    )
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--return-code", type=int)
    args = parser.parse_args()
    if args.command == "plan":
        value = plan(args.run_dir, args.ledger)
    elif args.command == "begin":
        value = begin(args.run_dir, args.ledger)
    elif args.command == "terminalize":
        value = terminalize(args.run_dir, args.ledger)
    elif args.command == "invoke":
        if args.owner_pid is None:
            parser.error("--owner-pid is required for invoke")
        value = mark_invoking(
            args.run_dir,
            args.ledger,
            owner_pid=args.owner_pid,
        )
    else:
        if args.return_code is None:
            parser.error("--return-code is required for result")
        value = record_result(
            args.run_dir,
            args.ledger,
            return_code=args.return_code,
        )
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return 0 if value.get("status") != "REFUSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
