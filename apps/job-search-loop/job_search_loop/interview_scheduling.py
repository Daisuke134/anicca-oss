from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .calendar_sync import event_key
from .interview_prep import PrepStore
from .recruiter_reply import send_reply_once
from .state import is_excluded_employer


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,1024}$")
MAX_SLOTS = 20
MAX_HORIZON = timedelta(days=180)
MIN_DURATION = timedelta(minutes=15)
MAX_DURATION = timedelta(hours=4)
_UNSET = object()


class SchedulingError(ValueError):
    pass


@dataclass(frozen=True)
class CandidateSlot:
    start: datetime
    end: datetime
    source_span: str


def _clean(value: Any, *, name: str, maximum: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if not cleaned:
        raise SchedulingError(f"{name} is required")
    if len(cleaned) > maximum:
        raise SchedulingError(f"{name} exceeds {maximum} characters")
    return cleaned


def _parse_timestamp(value: Any, *, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise SchedulingError(f"{name} must be RFC3339") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchedulingError(f"{name} requires an explicit timezone")
    return parsed


def normalize_candidate_slots(
    raw_slots: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[CandidateSlot]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise SchedulingError("now requires an explicit timezone")
    if not raw_slots or len(raw_slots) > MAX_SLOTS:
        raise SchedulingError(f"one to {MAX_SLOTS} candidate slots are required")
    slots: list[CandidateSlot] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_slots:
        if not isinstance(raw, dict):
            raise SchedulingError("candidate slot must be an object")
        start = _parse_timestamp(raw.get("start"), name="slot start")
        end = _parse_timestamp(raw.get("end"), name="slot end")
        source_span = _clean(
            raw.get("source_span"),
            name="slot source span",
            maximum=500,
        )
        duration = end - start
        if duration < MIN_DURATION or duration > MAX_DURATION:
            raise SchedulingError("slot duration must be between 15 minutes and 4 hours")
        if start <= now:
            raise SchedulingError("candidate slot must be in the future")
        if start - now > MAX_HORIZON:
            raise SchedulingError("candidate slot exceeds the 180-day horizon")
        identity = (start.isoformat(), end.isoformat())
        if identity not in seen:
            slots.append(CandidateSlot(start, end, source_span))
            seen.add(identity)
    return sorted(slots, key=lambda slot: slot.start)


def select_available_slot(
    slots: Iterable[CandidateSlot],
    busy_intervals: Iterable[tuple[datetime, datetime]],
) -> CandidateSlot | None:
    busy = list(busy_intervals)
    for slot in sorted(slots, key=lambda value: value.start):
        if not any(slot.start < end and slot.end > start for start, end in busy):
            return slot
    return None


def _run_json(argv: list[str]) -> Any:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"calendar transport failed rc={completed.returncode}: "
            f"{completed.stderr[-500:]}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("calendar transport returned invalid JSON") from error


def query_busy_intervals(
    *,
    account: str,
    slots: list[CandidateSlot],
    executable: str = "gog",
) -> list[tuple[datetime, datetime]]:
    if not slots:
        raise SchedulingError("candidate slots are required")
    account = _clean(account, name="account", maximum=254)
    value = _run_json(
        [
            executable,
            "calendar",
            "freebusy",
            "primary",
            "--account",
            account,
            "--json",
            "--no-input",
            "--from",
            min(slot.start for slot in slots).isoformat(),
            "--to",
            max(slot.end for slot in slots).isoformat(),
        ]
    )
    calendar = value.get("calendars", {}).get("primary", {})
    if calendar.get("errors"):
        raise RuntimeError("primary calendar freebusy query returned an error")
    intervals: list[tuple[datetime, datetime]] = []
    for item in calendar.get("busy", []):
        start = _parse_timestamp(item.get("start"), name="busy start")
        end = _parse_timestamp(item.get("end"), name="busy end")
        intervals.append((start, end))
    return intervals


def _thread_key(thread_id: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(thread_id):
        raise SchedulingError("Gmail thread ID is invalid")
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:24]


def _event_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("events", "items"):
            if isinstance(value.get(key), list):
                return [
                    item for item in value[key] if isinstance(item, dict)
                ]
    raise RuntimeError("Calendar event search returned an invalid result")


def _result_event_id(value: Any, fallback: str | None = None) -> str:
    candidates = [value]
    if isinstance(value, dict):
        candidates.extend(value.get(key) for key in ("event", "result"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            identifier = str(candidate.get("id") or "")
            if IDENTIFIER_PATTERN.fullmatch(identifier):
                return identifier
    if fallback and IDENTIFIER_PATTERN.fullmatch(fallback):
        return fallback
    raise RuntimeError("Calendar ACK has no valid event ID")


def _event_time(value: dict[str, Any], key: str) -> datetime | None:
    raw = value.get(key, {})
    if not isinstance(raw, dict):
        return None
    timestamp = raw.get("dateTime")
    if not timestamp:
        return None
    try:
        return _parse_timestamp(timestamp, name=f"event {key}")
    except SchedulingError:
        return None


def _event_arguments(
    *,
    summary: str,
    description: str,
    slot: CandidateSlot,
    thread_private_key: str,
    calendar_event_key: str,
) -> list[str]:
    return [
        "--summary",
        summary,
        "--from",
        slot.start.isoformat(),
        "--to",
        slot.end.isoformat(),
        "--description",
        description,
        "--visibility",
        "private",
        "--transparency",
        "busy",
        "--private-prop",
        f"anicca_job_thread={thread_private_key}",
        "--private-prop",
        f"anicca_job_event={calendar_event_key}",
        "--reminder",
        "popup:3d",
        "--reminder",
        "popup:1d",
    ]


def find_interview_event(
    *,
    account: str,
    thread_id: str,
    executable: str = "gog",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    account = _clean(account, name="account", maximum=254)
    current = now or datetime.now(tz=ZoneInfo("Asia/Tokyo"))
    if current.tzinfo is None or current.utcoffset() is None:
        raise SchedulingError("now requires an explicit timezone")
    private_thread_key = _thread_key(thread_id)
    rows = _event_rows(
        _run_json(
            [
                executable,
                "calendar",
                "events",
                "primary",
                "--account",
                account,
                "--json",
                "--no-input",
                "--results-only",
                "--from",
                (current - timedelta(days=1)).isoformat(),
                "--to",
                (current + timedelta(days=366)).isoformat(),
                "--private-prop-filter",
                f"anicca_job_thread={private_thread_key}",
                "--all-pages",
            ]
        )
    )
    if len(rows) > 1:
        raise RuntimeError("multiple Calendar events exist for one recruiting thread")
    return rows[0] if rows else None


def ensure_interview_event(
    *,
    account: str,
    thread_id: str,
    company: str,
    role: str,
    slot: CandidateSlot,
    executable: str = "gog",
    now: datetime | None = None,
    existing_event: dict[str, Any] | None | object = _UNSET,
) -> dict[str, str]:
    account = _clean(account, name="account", maximum=254)
    company = _clean(company, name="company", maximum=160)
    role = _clean(role, name="role", maximum=200)
    current = now or datetime.now(tz=ZoneInfo("Asia/Tokyo"))
    if current.tzinfo is None or current.utcoffset() is None:
        raise SchedulingError("now requires an explicit timezone")
    private_thread_key = _thread_key(thread_id)
    calendar_event_key = event_key(thread_id, slot.start)
    existing = (
        find_interview_event(
            account=account,
            thread_id=thread_id,
            executable=executable,
            now=current,
        )
        if existing_event is _UNSET
        else existing_event
    )

    summary = f"Interview: {company} — {role}"
    description = (
        "Confirmed from a recruiting email by Anicca Job Search Loop. "
        f"Private scheduling key: {calendar_event_key}."
    )
    event_arguments = _event_arguments(
        summary=summary,
        description=description,
        slot=slot,
        thread_private_key=private_thread_key,
        calendar_event_key=calendar_event_key,
    )
    if existing is not None:
        if not isinstance(existing, dict):
            raise RuntimeError("existing Calendar event is invalid")
        existing_id = _result_event_id(existing)
        existing_start = _event_time(existing, "start")
        existing_end = _event_time(existing, "end")
        if (
            existing_start == slot.start
            and existing_end == slot.end
            and existing.get("summary") == summary
        ):
            return {
                "action": "existing",
                "event_id": existing_id,
                "event_key": calendar_event_key,
            }
        value = _run_json(
            [
                executable,
                "calendar",
                "update",
                "primary",
                existing_id,
                "--account",
                account,
                "--json",
                "--no-input",
                *event_arguments,
            ]
        )
        return {
            "action": "updated",
            "event_id": _result_event_id(value, fallback=existing_id),
            "event_key": calendar_event_key,
        }

    value = _run_json(
        [
            executable,
            "calendar",
            "create",
            "primary",
            "--account",
            account,
            "--json",
            "--no-input",
            *event_arguments,
        ]
    )
    return {
        "action": "created",
        "event_id": _result_event_id(value),
        "event_key": calendar_event_key,
    }


def build_confirmation_reply(
    slot: CandidateSlot, *, candidate_name: str
) -> dict[str, Any]:
    japan = ZoneInfo("Asia/Tokyo")
    start = slot.start.astimezone(japan)
    end = slot.end.astimezone(japan)
    human_time = (
        f"{start.strftime('%A, %B')} {start.day}, {start.year}, "
        f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')} JST (UTC+09:00)"
    )
    return {
        "action": "auto_reply",
        "question_kind": "scheduling",
        "question_source_span": slot.source_span,
        "fact_ids": [],
        "body": (
            "Thank you for sharing the interview options.\n\n"
            f"I confirm {human_time}. I look forward to speaking with you.\n\n"
            f"Best regards,\n{candidate_name.strip()}"
        ),
    }


def confirm_interview_slot(
    *,
    database: Path,
    prep_database: Path,
    evidence_dir: Path,
    account: str,
    inbound_message_id: str,
    inbound_subject: str,
    thread_id: str,
    company: str,
    role: str,
    candidate_name: str,
    raw_slots: list[dict[str, Any]],
    now: datetime,
    calendar_executable: str = "gog",
    gmail_executable: str = "gog",
    allow_self_recipient: bool = False,
) -> dict[str, Any]:
    if is_excluded_employer(company):
        return {
            "status": "blocked",
            "blocker": "employer_excluded",
        }
    slots = normalize_candidate_slots(raw_slots, now=now)
    existing = find_interview_event(
        account=account,
        thread_id=thread_id,
        executable=calendar_executable,
        now=now,
    )
    selected = None
    if existing is not None:
        existing_start = _event_time(existing, "start")
        existing_end = _event_time(existing, "end")
        selected = next(
            (
                slot
                for slot in slots
                if slot.start == existing_start and slot.end == existing_end
            ),
            None,
        )
    if selected is None:
        busy = query_busy_intervals(
            account=account,
            slots=slots,
            executable=calendar_executable,
        )
        selected = select_available_slot(slots, busy)
    if selected is None:
        return {
            "status": "no_available_slot",
            "candidate_count": len(slots),
        }
    calendar = ensure_interview_event(
        account=account,
        thread_id=thread_id,
        company=company,
        role=role,
        slot=selected,
        executable=calendar_executable,
        now=now,
        existing_event=existing,
    )
    prep_store = PrepStore(prep_database)
    try:
        prep_interview_key = prep_store.register_interview(
            thread_id=thread_id,
            event_key=calendar["event_key"],
            company=company,
            role=role,
            start=selected.start,
            end=selected.end,
            registered_at=now,
        )
    finally:
        prep_store.close()
    reply = send_reply_once(
        database=database,
        evidence_dir=evidence_dir,
        account=account,
        inbound_message_id=inbound_message_id,
        inbound_subject=inbound_subject,
        decision=build_confirmation_reply(selected, candidate_name=candidate_name),
        executable=gmail_executable,
        allow_self_recipient=allow_self_recipient,
    )
    return {
        "status": "confirmed",
        "selected_start": selected.start.isoformat(),
        "selected_end": selected.end.isoformat(),
        "calendar_action": calendar["action"],
        "calendar_event_id": calendar["event_id"],
        "calendar_event_key": calendar["event_key"],
        "prep_interview_key": prep_interview_key,
        "prep_status": "pending_generation",
        "reply_status": reply["status"],
        "reply_message_id": reply["message_id"],
    }
