from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .outbox import Outbox
from .state import is_excluded_employer


AUTO_REPLY_KINDS = {
    "experience",
    "location",
    "desired_compensation",
    "contact",
}
BLOCKED_KINDS = {
    "work_authorization",
    "visa",
    "start_date",
    "current_compensation",
    "references",
    "legal",
}
MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class ReplyError(ValueError):
    pass


def _clean(value: Any, *, name: str, maximum: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if not cleaned:
        raise ReplyError(f"{name} is required")
    if len(cleaned) > maximum:
        raise ReplyError(f"{name} exceeds {maximum} characters")
    return cleaned


def _candidate(profile: dict[str, Any], name: str) -> Any:
    value = profile.get("candidate", {}).get(name)
    if value is None or value == "" or value == []:
        raise ReplyError(f"verified candidate field is missing: {name}")
    return value


def build_approved_reply(
    profile: dict[str, Any],
    *,
    question_kind: str,
    question_source_span: str,
    fact_ids: list[str] | None = None,
) -> dict[str, Any]:
    source_span = _clean(
        question_source_span,
        name="question source span",
        maximum=1_000,
    )
    if question_kind in BLOCKED_KINDS:
        return {
            "action": "blocked",
            "question_kind": question_kind,
            "reason": "verified_private_answer_missing_or_manual_legal_answer",
            "question_source_span": source_span,
            "fact_ids": [],
        }
    if question_kind == "scheduling":
        return {
            "action": "route_scheduling",
            "question_kind": question_kind,
            "reason": "owned_by_interview_scheduling_workflow",
            "question_source_span": source_span,
            "fact_ids": [],
        }
    if question_kind not in AUTO_REPLY_KINDS:
        raise ReplyError(f"unsupported question kind: {question_kind}")

    candidate = profile.get("candidate", {})
    selected_fact_ids: list[str] = []
    if question_kind == "experience":
        selected_fact_ids = list(fact_ids or [])
        if not selected_fact_ids or len(selected_fact_ids) > 4:
            raise ReplyError("experience reply requires one to four fact IDs")
        approved = {
            str(fact["id"]): str(fact["claim"])
            for fact in profile.get("facts", [])
            if fact.get("id") and fact.get("claim")
        }
        if not set(selected_fact_ids) <= set(approved):
            raise ReplyError("experience reply references an unapproved fact")
        answer = "\n".join(f"- {approved[fact_id]}" for fact_id in selected_fact_ids)
    elif question_kind == "location":
        base = _clean(
            _candidate(profile, "base"),
            name="candidate base",
            maximum=160,
        )
        preferences_value = _candidate(profile, "location_preferences")
        if not isinstance(preferences_value, list):
            raise ReplyError("location preferences must be a list")
        preferences = ", ".join(
            _clean(value, name="location preference", maximum=160)
            for value in preferences_value
        )
        answer = f"I am based in {base}. My location preferences are {preferences}."
    elif question_kind == "desired_compensation":
        compensation = _clean(
            _candidate(profile, "desired_compensation_jpy"),
            name="desired compensation",
            maximum=160,
        )
        answer = f"My desired compensation range is {compensation}."
    else:
        email = _clean(
            _candidate(profile, "application_email"),
            name="application email",
            maximum=254,
        )
        phone = _clean(
            _candidate(profile, "phone"),
            name="phone",
            maximum=80,
        )
        answer = f"The best contact details are {email} and {phone}."

    body = (
        "Thank you for your message.\n\n"
        f"{answer}\n\n"
        "Please let me know if any additional verified information would be helpful.\n\n"
        "Best regards,\n"
        + _clean(_candidate(profile, "name"), name="candidate name", maximum=160)
    )
    return {
        "action": "auto_reply",
        "question_kind": question_kind,
        "body": body,
        "question_source_span": source_span,
        "fact_ids": selected_fact_ids,
    }


def is_safe_recruiter_message(headers: Mapping[str, str]) -> bool:
    normalized = {str(key).casefold(): str(value) for key, value in headers.items()}
    sender = normalized.get("from", "").casefold()
    if not sender or "no-reply" in sender or "noreply" in sender:
        return False
    auto_submitted = normalized.get("auto-submitted", "").strip().casefold()
    if auto_submitted and auto_submitted != "no":
        return False
    if normalized.get("list-id", "").strip():
        return False
    if normalized.get("precedence", "").strip().casefold() in {"bulk", "list", "junk"}:
        return False
    return True


def _reply_subject(subject: str) -> str:
    subject = _clean(subject, name="inbound subject", maximum=300)
    return subject if subject.casefold().startswith("re:") else f"Re: {subject}"


def send_reply_once(
    *,
    database: Path,
    evidence_dir: Path,
    account: str,
    inbound_message_id: str,
    inbound_subject: str,
    decision: dict[str, Any],
    executable: str = "gog",
    allow_self_recipient: bool = False,
) -> dict[str, str | None]:
    if decision.get("action") != "auto_reply":
        raise ReplyError("only approved auto_reply decisions may be sent")
    decision_company = decision.get("company")
    if decision_company and is_excluded_employer(str(decision_company)):
        raise ReplyError("employer is excluded")
    message_id = str(inbound_message_id)
    if not MESSAGE_ID_PATTERN.fullmatch(message_id):
        raise ReplyError("inbound Gmail message ID is invalid")
    account = _clean(account, name="account", maximum=254)
    subject = _reply_subject(inbound_subject)
    body = _clean(decision.get("body"), name="reply body", maximum=4_000)
    event_key = f"gmail-reply:{message_id}"
    payload = json.dumps(
        {
            "message_id": message_id,
            "subject": subject,
            "body": body,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    outbox = Outbox(database)
    try:
        outbox.enqueue(event_key, payload)
        existing = outbox.status(event_key)
        if existing["status"] == "sent":
            return existing
        fence = outbox.claim(event_key)
        evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(evidence_dir, 0o700)
        body_path = evidence_dir / f"reply-{message_id}.txt"
        body_path.write_text(body + "\n", encoding="utf-8")
        os.chmod(body_path, 0o600)
        outbox.mark_send_started(event_key, fence)
        recipient_arguments = (
            ["--to", account] if allow_self_recipient else ["--reply-all"]
        )
        completed = subprocess.run(
            [
                executable,
                "gmail",
                "send",
                "--account",
                account,
                "--json",
                "--no-input",
                "--reply-to-message-id",
                message_id,
                *recipient_arguments,
                "--subject",
                subject,
                "--body-file",
                str(body_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            stderr_path = evidence_dir / f"reply-{message_id}.stderr.log"
            stderr_path.write_text(completed.stderr[-2_000:], encoding="utf-8")
            os.chmod(stderr_path, 0o600)
            raise RuntimeError(
                f"Gmail reply transport failed rc={completed.returncode}"
            )
        result = json.loads(completed.stdout)
        sent_message_id = (
            result.get("messageId")
            or result.get("id")
            or result.get("message", {}).get("id")
        )
        if not sent_message_id:
            raise RuntimeError("Gmail reply ACK has no message ID")
        outbox.mark_sent(event_key, fence, str(sent_message_id))
        return outbox.status(event_key)
    finally:
        outbox.close()
