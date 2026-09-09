#!/usr/bin/env python3
"""Receipt-backed, provider-neutral marketplace application transaction."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import importlib.util
import json
import os
from datetime import date
from pathlib import Path
import re
import sys
import tempfile
from typing import Callable, Dict, Mapping, Optional, Set, Tuple


_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class TickResult:
    ok: bool
    submitted: bool = False
    application_verified: bool = False
    error: Optional[str] = None
    reason: Optional[str] = None
    project_id: Optional[str] = None
    provider_proposal_id: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        result: Dict[str, object] = {
            "ok": self.ok,
            "submitted": self.submitted,
            "application_verified": self.application_verified,
        }
        for key in ("error", "reason", "project_id", "provider_proposal_id"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        return result


class _AccountLockBusy(RuntimeError):
    pass


class _StateInvalid(RuntimeError):
    pass


_SUBMISSION_NOT_STARTED_ERRORS = frozenset({
    "proposal_form_changed",
    "financial_terms_required",
})


class SubmissionNotStarted(RuntimeError):
    """A validated provider-side failure that happened before final submission."""

    def __init__(self, error: str):
        if not isinstance(error, str) or error not in _SUBMISSION_NOT_STARTED_ERRORS:
            raise ValueError("invalid_submission_not_started_error")
        self.error = self.code = error
        super().__init__(error)


_LEGACY_PENDING_FIELDS = frozenset({
    "proposal_id",
    "content_sha256",
    "amount_minor",
    "delivery_due_on",
})
_PENDING_FIELDS = _LEGACY_PENDING_FIELDS | {"project_id"}
_HOURLY_PENDING_FIELDS = _PENDING_FIELDS | {"pricing_mode", "weekly_limit_hours"}


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _is_delivery_due_on(value: object) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return parsed.isoformat() == value


def _is_project_id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


_OPTIONAL_PENDING_FIELDS = {"title"}


def _read_state(path: Path) -> Tuple[Set[str], Dict[str, Dict[str, object]]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set(), {}
    except (OSError, ValueError, TypeError) as exc:
        raise _StateInvalid() from exc
    if not isinstance(value, Mapping):
        raise _StateInvalid()
    if set(value) not in ({"fingerprints"}, {"fingerprints", "pending"}):
        raise _StateInvalid()

    raw_fingerprints = value["fingerprints"]
    if not isinstance(raw_fingerprints, list):
        raise _StateInvalid()
    fingerprints: Set[str] = set()
    for item in raw_fingerprints:
        if not _is_sha256(item):
            raise _StateInvalid()
        fingerprints.add(item)

    raw_pending = value.get("pending", {})
    if not isinstance(raw_pending, Mapping):
        raise _StateInvalid()
    pending: Dict[str, Dict[str, object]] = {}
    for marker, raw_entry in raw_pending.items():
        if not _is_sha256(marker) or marker not in fingerprints:
            raise _StateInvalid()
        # The title is optional and carried only so a later reconcile can name the job, so compare
        # the required fields rather than the exact set: an exact-set check rejected the whole state
        # file the moment a claim carried one, and every reconcile returned state_invalid.
        entry_fields = set(raw_entry) - _OPTIONAL_PENDING_FIELDS if isinstance(raw_entry, Mapping) else set()
        if not isinstance(raw_entry, Mapping) or entry_fields not in (
            _LEGACY_PENDING_FIELDS,
            _PENDING_FIELDS,
            _HOURLY_PENDING_FIELDS,
        ):
            raise _StateInvalid()
        entry_title = raw_entry.get("title")
        if entry_title is not None and (not isinstance(entry_title, str) or not entry_title.strip()):
            raise _StateInvalid()
        proposal_id = raw_entry["proposal_id"]
        if proposal_id is not None and (
            not isinstance(proposal_id, str) or not proposal_id.strip()
        ):
            raise _StateInvalid()
        content_sha256 = raw_entry["content_sha256"]
        amount_minor = raw_entry["amount_minor"]
        delivery_due_on = raw_entry["delivery_due_on"]
        project_id = raw_entry.get("project_id")
        pricing_mode = raw_entry.get("pricing_mode", "fixed")
        weekly_limit_hours = raw_entry.get("weekly_limit_hours")
        terms_valid = (
            pricing_mode == "fixed"
            and _is_delivery_due_on(delivery_due_on)
            and weekly_limit_hours is None
        ) or (
            pricing_mode == "hourly"
            and delivery_due_on is None
            and isinstance(weekly_limit_hours, int)
            and not isinstance(weekly_limit_hours, bool)
            and weekly_limit_hours > 0
        )
        if (
            not _is_sha256(content_sha256)
            or isinstance(amount_minor, bool)
            or not isinstance(amount_minor, int)
            or amount_minor <= 0
            or not terms_valid
            or (entry_fields != _LEGACY_PENDING_FIELDS and not _is_project_id(project_id))
        ):
            raise _StateInvalid()
        entry = {
            "proposal_id": proposal_id,
            "content_sha256": content_sha256,
            "amount_minor": amount_minor,
            "delivery_due_on": delivery_due_on,
        }
        # Rebuilding the entry field by field is the third place the title was dropped, after the
        # writer's field list and the reader's exact-set check.
        if isinstance(entry_title, str) and entry_title.strip(): entry["title"] = entry_title.strip()
        if entry_fields != _LEGACY_PENDING_FIELDS:
            entry["project_id"] = project_id
        if entry_fields == _HOURLY_PENDING_FIELDS:
            entry["pricing_mode"] = pricing_mode
            entry["weekly_limit_hours"] = weekly_limit_hours
        pending[marker] = entry
    return fingerprints, pending


def _write_state(
    path: Path, fingerprints: Set[str], pending: Mapping[str, Mapping[str, object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending_payload = {
        marker: {
            **{field: entry[field] for field in _LEGACY_PENDING_FIELDS},
            **({"project_id": entry["project_id"]} if "project_id" in entry else {}),
            **({"pricing_mode": entry["pricing_mode"], "weekly_limit_hours": entry["weekly_limit_hours"]} if "pricing_mode" in entry else {}),
            # The claim carries the job's name so a later reconcile can name it; dropping it here
            # is why receipts read "案件: 案件 13422653" instead of the job.
            **({"title": entry["title"]} if "title" in entry else {}),
        }
        for marker, entry in sorted(pending.items())
    }
    payload = {"fingerprints": sorted(fingerprints), "pending": pending_payload}
    fd, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix="." + path.name + "."
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


@contextmanager
def account_lock(state_path: Path):
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(path.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def require_project_id(opportunity: Mapping[str, object]) -> str:
    value = opportunity.get("external_id") if isinstance(opportunity, Mapping) else None
    if not _is_project_id(value):
        raise ValueError("project_id_required")
    return value


def read_pending_descriptors(state_path: Path) -> list[Dict[str, object]]:
    _, pending = _read_state(Path(state_path))
    return [
        {
            key: pending[marker][key]
            for key in ("project_id", "amount_minor", "delivery_due_on", "pricing_mode", "weekly_limit_hours")
            if key in pending[marker]
        }
        for marker in sorted(pending)
        if _is_project_id(pending[marker].get("project_id"))
    ]


def read_pending_descriptor(state_path: Path) -> Optional[Dict[str, object]]:
    values = read_pending_descriptors(state_path)
    return values[0] if values else None


def load_marketplace_contracts():
    name = "anicca_marketplace_core_contracts_application_transaction"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).resolve().with_name("contracts.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("marketplace_contracts_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _valid_terms(proposed_amount_minor: object, delivery_due_on: object, pricing_mode: object, weekly_limit_hours: object) -> bool:
    amount_valid = (
        isinstance(proposed_amount_minor, int)
        and not isinstance(proposed_amount_minor, bool)
        and proposed_amount_minor > 0
    )
    return amount_valid and (
        (pricing_mode == "fixed" and _is_delivery_due_on(delivery_due_on) and weekly_limit_hours is None)
        or (
            pricing_mode == "hourly"
            and delivery_due_on is None
            and isinstance(weekly_limit_hours, int)
            and not isinstance(weekly_limit_hours, bool)
            and weekly_limit_hours > 0
        )
    )


def _optional_proposal_id(value: object) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("proposal_id")
    if isinstance(value, str) and value.strip():
        return value
    return None


def _observed_proposal_id(observed: object) -> Optional[str]:
    if not isinstance(observed, Mapping):
        return None
    return _optional_proposal_id(observed.get("proposal_id"))


def _uncertain(project_id: str, proposal_id: Optional[str] = None) -> TickResult:
    return TickResult(
        ok=False,
        error="submission_uncertain",
        project_id=project_id,
        provider_proposal_id=proposal_id,
    )


def _reconcile_pending(
    *,
    path: Path,
    marker: str,
    claims: Set[str],
    pending: Dict[str, Dict[str, object]],
    pending_entry: Dict[str, object],
    project_id: str,
    platform: str,
    readback: Callable[[Optional[str], str], Mapping[str, object]],
    ledger_writer: Callable[[Mapping[str, object]], object],
    now: Callable[[], object],
    submitted: bool,
) -> TickResult:
    proposal_id = pending_entry["proposal_id"]
    try:
        observed = readback(proposal_id, project_id)
    except Exception:
        return _uncertain(project_id, proposal_id if isinstance(proposal_id, str) else None)

    observed_proposal_id = _observed_proposal_id(observed)
    pricing_mode = pending_entry.get("pricing_mode", "fixed")
    observed_amount = observed.get("hourly_rate_minor" if pricing_mode == "hourly" else "amount_minor") if isinstance(observed, Mapping) else None
    terms_match = isinstance(observed, Mapping) and observed.get("project_id") == project_id and (
        pricing_mode == "fixed"
        and isinstance(observed_amount, int)
        and not isinstance(observed_amount, bool)
        and observed_amount == pending_entry["amount_minor"]
        and observed.get("delivery_due_on") == pending_entry["delivery_due_on"]
        or pricing_mode == "hourly"
        and observed.get("pricing_mode") == "hourly"
        and observed_amount == pending_entry["amount_minor"]
        and observed.get("weekly_limit_hours") == pending_entry["weekly_limit_hours"]
    )
    proposal_match = (
        observed_proposal_id is not None
        if proposal_id is None
        else observed_proposal_id == proposal_id
    )
    if not terms_match or not proposal_match:
        return _uncertain(
            project_id,
            proposal_id if isinstance(proposal_id, str) else observed_proposal_id,
        )

    if proposal_id is None:
        proposal_id = observed_proposal_id
        if proposal_id is None:
            return _uncertain(project_id)
        pending_entry["proposal_id"] = proposal_id
        pending[marker] = pending_entry
        try:
            _write_state(path, claims, pending)
        except Exception:
            return _uncertain(project_id, proposal_id)

    try:
        receipt = {
            "schema_version": 1,
            "record_type": "application_receipt",
            "platform": platform,
            "opportunity_external_id": project_id,
            "application_external_id": proposal_id,
            "status": "verified",
            "content_sha256": pending_entry["content_sha256"],
            "idempotency_key": f"{platform}:application_receipt:{proposal_id}:v1",
            "observed_at": now(),
        }
        # A receipt without the job's name forces every reader to report a bare id, which is why
        # CrowdWorks reports read "案件: 案件 13422653" while Lancers names the job.
        title = pending_entry.get("title")
        if isinstance(title, str) and title.strip(): receipt["opportunity_title"] = title.strip()[:300]
        amount = pending_entry.get("amount_minor")
        if pricing_mode == "hourly":
            receipt.update({"pricing_mode": "hourly", "proposed_hourly_rate_minor": amount, "weekly_limit_hours": pending_entry["weekly_limit_hours"]})
        elif isinstance(amount, int) and not isinstance(amount, bool) and amount > 0:
            receipt["proposed_amount_minor"] = amount
        load_marketplace_contracts().parse_application_receipt(receipt)
        ledger_writer(receipt)
        pending.pop(marker, None)
        _write_state(path, claims, pending)
    except Exception:
        return _uncertain(project_id, proposal_id)
    return TickResult(
        ok=True,
        submitted=submitted,
        application_verified=True,
        project_id=project_id,
        provider_proposal_id=proposal_id,
    )


def run_transaction(
    *,
    platform: str,
    opportunity: Mapping[str, object],
    proposal_text: str,
    proposed_amount_minor: int,
    delivery_due_on: Optional[str],
    state_path: Path,
    account_ready: Callable[[], bool],
    submitter: Callable[..., object],
    readback: Callable[[Optional[str], str], Mapping[str, object]],
    ledger_writer: Callable[[Mapping[str, object]], object],
    now: Callable[[], object],
    pricing_mode: str = "fixed",
    weekly_limit_hours: Optional[int] = None,
) -> TickResult:
    if not isinstance(platform, str) or _PLATFORM_RE.fullmatch(platform) is None:
        raise ValueError("invalid_platform")
    try:
        project_id = require_project_id(opportunity)
    except (TypeError, ValueError):
        return TickResult(ok=False, error="project_id_required")
    if not isinstance(proposal_text, str) or not proposal_text:
        return TickResult(ok=False, error="proposal_text_required", project_id=project_id)
    if not _valid_terms(proposed_amount_minor, delivery_due_on, pricing_mode, weekly_limit_hours):
        return TickResult(ok=False, error="financial_terms_required", project_id=project_id)

    path = Path(state_path)
    marker = hashlib.sha256(f"{platform}:application:{project_id}".encode()).hexdigest()
    content_sha256 = hashlib.sha256(proposal_text.encode("utf-8")).hexdigest()
    try:
        existing_claims, existing_pending = _read_state(path)
        if marker in existing_claims and marker not in existing_pending:
            return TickResult(ok=True, reason="duplicate_project", project_id=project_id)
        if not account_ready():
            return TickResult(ok=False, error="account_unavailable", project_id=project_id)
        with account_lock(path):
            claims, pending = _read_state(path)
            pending_entry = pending.get(marker)
            if marker in claims and pending_entry is None:
                return TickResult(ok=True, reason="duplicate_project", project_id=project_id)

            submitted_now = False
            if marker not in claims:
                pending_entry = {
                    "proposal_id": None,
                    "content_sha256": content_sha256,
                    "amount_minor": proposed_amount_minor,
                    "delivery_due_on": delivery_due_on,
                    "project_id": project_id,
                }
                if pricing_mode == "hourly":
                    pending_entry.update({"pricing_mode": "hourly", "weekly_limit_hours": weekly_limit_hours})
                # Carry the job's name with the claim so a later reconcile, which only knows ids,
                # can still write a receipt a human can read.
                claim_title = opportunity.get("title") if isinstance(opportunity, Mapping) else None
                if isinstance(claim_title, str) and claim_title.strip(): pending_entry["title"] = claim_title.strip()[:300]
                claims.add(marker)
                pending[marker] = pending_entry
                try:
                    _write_state(path, claims, pending)
                except Exception:
                    return TickResult(ok=False, error="state_invalid", project_id=project_id)

                try:
                    submitted = submitter(
                        opportunity,
                        proposal_text,
                        proposed_amount_minor,
                        delivery_due_on,
                    )
                    proposal_id = _optional_proposal_id(submitted)
                except SubmissionNotStarted as exc:
                    claims.discard(marker)
                    pending.pop(marker, None)
                    try:
                        _write_state(path, claims, pending)
                    except Exception:
                        return TickResult(ok=False, error="state_invalid", project_id=project_id)
                    return TickResult(ok=False, error=exc.error, project_id=project_id)
                except Exception:
                    return _uncertain(project_id)
                if proposal_id is None:
                    return _uncertain(project_id)
                pending_entry["proposal_id"] = proposal_id
                try:
                    _write_state(path, claims, pending)
                except Exception:
                    return _uncertain(project_id, proposal_id)
                submitted_now = True
            else:
                # A fingerprint with a pending entry is a claimed transaction;
                # its provider boundary is readback-only on every later tick.
                pending_entry = dict(pending_entry)

            return _reconcile_pending(
                path=path,
                marker=marker,
                claims=claims,
                pending=pending,
                pending_entry=pending_entry,
                project_id=project_id,
                platform=platform,
                readback=readback,
                ledger_writer=ledger_writer,
                now=now,
                submitted=submitted_now,
            )
    except _AccountLockBusy:
        return TickResult(ok=False, error="account_lock_busy", project_id=project_id)
    except _StateInvalid:
        return TickResult(ok=False, error="state_invalid", project_id=project_id)


__all__ = [
    "SubmissionNotStarted",
    "TickResult",
    "account_lock",
    "load_marketplace_contracts",
    "read_pending_descriptor",
    "read_pending_descriptors",
    "run_transaction",
]
