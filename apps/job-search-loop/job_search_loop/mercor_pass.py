from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent_runner import AgentRunner, PassAlreadyRunning
from .mercor_provider import run_pass
from .mercor_submit_guard import fenced_listing_ids


def _shared_apply_context(profile_path: Path) -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[3]
        / "skills/_shared/marketplace-core/scripts/apply_policy.py"
    )
    spec = importlib.util.spec_from_file_location("marketplace_shared_apply_policy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("marketplace_apply_policy_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_apply_context(profile_path)


def _mercor_auth_context(profile_path: Path) -> dict[str, str]:
    value = json.loads(profile_path.expanduser().read_text(encoding="utf-8"))
    candidate = value.get("candidate", {}) if isinstance(value, dict) else {}
    email = candidate.get("application_email", "") if isinstance(candidate, dict) else ""
    if not isinstance(email, str) or not email.strip():
        raise ValueError("mercor_account_email_unavailable")
    return {"login_method": "email", "account_email": email.strip()}


def _host_capabilities() -> dict[str, Any]:
    architecture = platform.machine()
    macos_version = platform.mac_ver()[0]
    try:
        macos_major = int(macos_version.split(".", 1)[0])
    except (ValueError, IndexError):
        macos_major = 0
    return {
        "architecture": architecture,
        "macos_version": macos_version,
        "apple_silicon": architecture == "arm64",
        "macos_sequoia_or_newer": macos_major >= 15,
    }


def _ledger_listing_ids(path: Path) -> list[str]:
    if not path.is_file():
        return []
    identifiers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        listing_id = value.get("listing_id")
        if isinstance(listing_id, str) and listing_id.strip():
            identifiers.append(listing_id.strip())
    return sorted(set(identifiers))


def _recent_listing_ids(path: Path, limit: int = 200) -> list[str]:
    if not path.is_file():
        return []
    identifiers: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        listing_id = value.get("listing_id") if isinstance(value, dict) else None
        if isinstance(listing_id, str) and listing_id.strip() and listing_id != "unavailable":
            identifiers.append(listing_id.strip())
    return list(dict.fromkeys(identifiers))


def build_context(
    *,
    state_root: Path,
    profile_path: Path,
    resume_path: Path,
    cdp_url: str,
    evidence_dir: Path | None = None,
    run_id: str = "",
    cdp_page_ws: str = "",
) -> dict[str, Any]:
    ledger = state_root / "applications.jsonl"
    fence_ledger = state_root / "submission-fences.jsonl"
    inspection_ledger = state_root / "inspections.jsonl"
    submitted_listing_ids = set(_ledger_listing_ids(ledger))
    submitted_listing_ids.update(fenced_listing_ids(fence_ledger))
    context = {
        "operator_id": os.environ.get("MERCOR_OPERATOR_ID", "default"),
        "state_root": str(state_root.resolve()),
        "profile_path": str(profile_path.expanduser().resolve()),
        "resume_path": str(resume_path.expanduser().resolve()),
        "applications_ledger": str(ledger.resolve()),
        "submission_fence_ledger": str(fence_ledger.resolve()),
        "application_report_outbox": str((state_root / "telegram.sqlite3").resolve()),
        "application_report_telegram_env": str(
            (Path.home() / ".config/anicca/job-search/telegram.env").resolve()
        ),
        "capability_catalog_path": str(
            (Path(__file__).resolve().parents[3]
             / "skills/gig-work/profile/listings/catalog.json").resolve()
        ),
        "submitted_listing_ids": sorted(submitted_listing_ids),
        "recently_inspected_listing_ids": _recent_listing_ids(inspection_ledger),
        "shared_apply_context": _shared_apply_context(profile_path),
        "mercor_auth_context": _mercor_auth_context(profile_path),
        "host_capabilities": _host_capabilities(),
        "run_id": run_id,
        "cdp_url": cdp_url,
        "cdp_page_ws": cdp_page_ws,
    }
    if evidence_dir is not None:
        context["evidence_dir"] = str(evidence_dir.expanduser().resolve())
    return context


def record_verified_submissions(state_root: Path, result: dict[str, Any], *, run_id: str) -> None:
    submitted = result.get("submitted")
    if result.get("status") != "submitted" or not isinstance(submitted, list):
        return
    ledger = state_root / "applications.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = ledger.with_name(f"{ledger.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        known = set(_ledger_listing_ids(ledger))
        with ledger.open("a", encoding="utf-8") as output:
            for item in submitted:
                listing_id = item.get("listing_id") if isinstance(item, dict) else None
                if not isinstance(listing_id, str) or not listing_id.strip() or listing_id in known:
                    continue
                row = {
                    "listing_id": listing_id,
                    "title": item.get("title", ""),
                    "application_url": item.get("evidence_url") or item.get("url", ""),
                    "status": "submitted_pending_review",
                    "evidence_path": item.get("evidence_path", ""),
                    "run_id": run_id,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                }
                output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                known.add(listing_id)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(ledger, 0o600)


def record_inspections(state_root: Path, result: dict[str, Any], *, run_id: str) -> None:
    inspected = result.get("inspected_listings")
    if not isinstance(inspected, list):
        return
    ledger = state_root / "inspections.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ledger.open("a", encoding="utf-8") as output:
        for item in inspected:
            if not isinstance(item, dict):
                continue
            listing_id = item.get("listing_id")
            if not isinstance(listing_id, str) or not listing_id.strip():
                continue
            output.write(json.dumps({
                "listing_id": listing_id.strip(),
                "decision": str(item.get("decision") or ""),
                "run_id": run_id,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(ledger, 0o600)


def validate_evidence_paths(result: dict[str, Any], evidence_root: Path) -> None:
    """Reject model evidence that escapes the private directory for this pass.

    Empty paths are allowed for a read-only pass that produced no artifact. Any
    non-empty path must resolve to an existing regular file beneath the current
    pass root; this prevents stale evidence from an older run being accepted as
    proof for the current run.
    """
    root = evidence_root.expanduser().resolve()
    evidence = result.get("evidence")
    if not isinstance(evidence, dict):
        return

    candidates: list[tuple[str, str]] = []
    for field in ("screenshot_path", "dom_path"):
        value = evidence.get(field)
        if isinstance(value, str) and value.strip():
            candidates.append((f"evidence.{field}", value.strip()))

    submitted = result.get("submitted")
    if isinstance(submitted, list):
        for index, item in enumerate(submitted):
            if not isinstance(item, dict):
                continue
            value = item.get("evidence_path")
            if isinstance(value, str) and value.strip():
                candidates.append((f"submitted[{index}].evidence_path", value.strip()))

    if submitted and not candidates:
        raise ValueError("submitted_result_missing_evidence_path")

    for label, raw_path in candidates:
        resolved = Path(raw_path).expanduser().resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError(f"{label}_outside_current_pass") from error
        if not resolved.is_file():
            raise ValueError(f"{label}_missing")


def validate_bounded_scan(result: dict[str, Any]) -> None:
    """Do not accept a model's early exit while its evidence exposes a full queue."""
    if result.get("status") == "blocked":
        return
    evidence = result.get("evidence")
    dom_path = evidence.get("dom_path") if isinstance(evidence, dict) else None
    if not isinstance(dom_path, str) or not dom_path.strip():
        return
    try:
        visible_ids = set(re.findall(
            r"listingId(?:=|%3D)(list_[A-Za-z0-9_-]+)",
            Path(dom_path).read_text(encoding="utf-8", errors="replace"),
        ))
    except OSError:
        return
    inspected = {
        item.get("listing_id")
        for item in result.get("inspected_listings", [])
        if isinstance(item, dict) and isinstance(item.get("listing_id"), str)
    }
    required = min(12, len(visible_ids))
    if required and len(inspected) < required:
        raise ValueError(f"bounded_scan_incomplete:{len(inspected)}_of_{required}")


def validate_priority_scan(result: dict[str, Any], evidence_root: Path) -> None:
    """Require observed Japanese and resumable candidates before a successful pass."""
    if result.get("status") == "blocked":
        return
    inspected = {
        item.get("listing_id")
        for item in result.get("inspected_listings", [])
        if isinstance(item, dict) and isinstance(item.get("listing_id"), str)
    }
    required: set[str] = set()
    card = re.compile(
        r'href=["\\]+/explore\?listingId=(list_[A-Za-z0-9_-]+).*?'
        r'data-test=["\\]+listing-title["\\]+[^>]*>([^<]+)',
        re.IGNORECASE | re.DOTALL,
    )
    for path in evidence_root.rglob("*.json"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for listing_id, title in card.findall(text):
            if re.search(r"Japanese|Japan|日本語", title, re.IGNORECASE):
                required.add(listing_id)
    missing = sorted(required - inspected)
    if missing:
        raise ValueError(f"priority_scan_incomplete:{','.join(missing)}")


def _blocked_for_evidence_violation(
    result: dict[str, Any], evidence_dir: Path, error: ValueError
) -> dict[str, Any]:
    """Preserve a rejected model result privately while keeping the wake reportable."""
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(evidence_dir, 0o700)
    violation_path = evidence_dir / "evidence-validation-error.json"
    violation_path.write_text(
        json.dumps({"error": str(error), "agent_result": result}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    os.chmod(violation_path, 0o600)
    evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
    blocked = result.get("blocked") if isinstance(result.get("blocked"), list) else []
    needs_human = result.get("needs_human") if isinstance(result.get("needs_human"), list) else []
    inspected = result.get("inspected_listings") if isinstance(result.get("inspected_listings"), list) else []
    return {
        "status": "blocked",
        "inspected_listings": inspected,
        "submitted": [],
        "needs_human": needs_human,
        "blocked": [*blocked, f"evidence_validation:{error}"],
        "evidence": {
            "page_url": evidence.get("page_url", ""),
            "screenshot_path": "",
            "dom_path": str(violation_path),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--resume", required=True, type=Path)
    parser.add_argument("--cdp-url", required=True)
    parser.add_argument("--cdp-page-ws", default="")
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    args.evidence_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(args.evidence_dir, 0o700)
    runner = AgentRunner(evidence_root=args.evidence_dir.parent)
    try:
        context = build_context(
            state_root=args.state_root,
            profile_path=args.profile,
            resume_path=args.resume,
            cdp_url=args.cdp_url,
            evidence_dir=args.evidence_dir.parent / args.run_id,
            run_id=args.run_id,
            cdp_page_ws=args.cdp_page_ws,
        )
        result = run_pass(
            runner=runner,
            prompt_path=args.prompt,
            schema_path=args.schema,
            context=context,
            workdir=args.workdir,
            run_id=args.run_id,
        )
    except PassAlreadyRunning:
        print("LIFE_MANAGER_PROVIDER_LEASE_BUSY", file=sys.stderr)
        return 75
    try:
        validate_evidence_paths(result, args.evidence_dir.parent)
        validate_bounded_scan(result)
        validate_priority_scan(result, args.evidence_dir.parent)
    except ValueError as error:
        result = _blocked_for_evidence_violation(result, args.evidence_dir, error)
    record_verified_submissions(args.state_root, result, run_id=args.run_id)
    record_inspections(args.state_root, result, run_id=args.run_id)
    output = args.evidence_dir / "mercor-pass-summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(output, 0o600)
    print(json.dumps({"status": result.get("status"), "result_path": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
