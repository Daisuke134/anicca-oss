from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import unicodedata
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .inbox import mark_messages_seen, message_is_seen
from .ledger import FenceError, Ledger


_WRAPPED_CONTENT = re.compile(
    r'\A<<<EXTERNAL_UNTRUSTED_CONTENT id="[^"]+">>>\n'
    r"Source: google_api\n---\n"
    r"(.*)\n"
    r'<<<END_EXTERNAL_UNTRUSTED_CONTENT id="[^"]+">>>\Z',
    re.DOTALL,
)
_CONFIRMATION_TERMS = (
    "applicationreceived",
    "thankyouforapplying",
    "thanksforapplying",
    "thankyouforyouronlinesubmission",
    "thankyouforyourinterest",
    "applicationsubmitted",
    "wehavereceivedyourapplication",
    "wevereceivedyourapplication",
    "wevegotyourapplication",
    "hasbeenreceived",
    "応募が完了",
    "応募を受け付け",
    "応募を受付",
    "ご応募いただ",
    "ご応募ありがとう",
    "エントリーが完了",
    "応募受付",
)


def _unwrap(value: Any) -> str:
    text = str(value or "").strip()
    match = _WRAPPED_CONTENT.fullmatch(text)
    return match.group(1).strip() if match else text


def _fold(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _sender_matches(sender: str, canonical_url: str) -> bool:
    sender_address = parseaddr(sender)[1].casefold()
    if "@" not in sender_address:
        return False
    sender_host = sender_address.rsplit("@", 1)[1].rstrip(".")
    job_host = (urlsplit(canonical_url).hostname or "").casefold().rstrip(".")
    if not sender_host or not job_host:
        return False
    if job_host == "jobs.ashbyhq.com":
        return sender_host == "ashbyhq.com" or sender_host.endswith(
            ".ashbyhq.com"
        )
    if "workday" in job_host:
        return sender_host in {"myworkday.com", "myworkdayjobs.com"} or any(
            sender_host.endswith(f".{domain}")
            for domain in ("myworkday.com", "myworkdayjobs.com")
        )
    return (
        sender_host == job_host
        or sender_host.endswith(f".{job_host}")
        or job_host.endswith(f".{sender_host}")
    )


def _message_from_payload(value: dict[str, Any]) -> dict[str, Any] | None:
    message_id = str(value.get("id") or "")
    thread_id = str(value.get("threadId") or "")
    headers = value.get("headers")
    if not isinstance(headers, dict):
        return None
    try:
        received_at = datetime.fromtimestamp(
            int(value.get("internalDate")) / 1000,
            tz=timezone.utc,
        ).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", message_id):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id):
        return None
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "sender": _unwrap(headers.get("from")),
        "recipient": _unwrap(
            headers.get("delivered-to")
            or headers.get("to")
            or headers.get("x-original-to")
        ),
        "subject": _unwrap(headers.get("subject")),
        "body": _unwrap(value.get("body")),
        "received_at": received_at,
    }


def _confirmation_matches(
    message: dict[str, str],
    candidate: dict[str, str],
    expected_recipient: str | None = None,
) -> bool:
    subject_and_body = _fold(f"{message['subject']}\n{message['body']}")
    company_context = _fold(
        f"{message['sender']}\n{message['subject']}\n{message['body']}"
    )
    return (
        (
            expected_recipient is None
            or parseaddr(message["recipient"])[1].casefold()
            == expected_recipient.casefold()
        )
        and any(term in subject_and_body for term in _CONFIRMATION_TERMS)
        and _fold(candidate["company"]) in company_context
        and _fold(candidate["title"]) in subject_and_body
        and _sender_matches(message["sender"], candidate["canonical_url"])
    )


def _confirmation_matches_without_title(
    message: dict[str, str],
    candidate: dict[str, str],
    expected_recipient: str | None = None,
) -> bool:
    subject_and_body = _fold(f"{message['subject']}\n{message['body']}")
    company_context = _fold(
        f"{message['sender']}\n{message['subject']}\n{message['body']}"
    )
    return (
        (
            expected_recipient is None
            or parseaddr(message["recipient"])[1].casefold()
            == expected_recipient.casefold()
        )
        and any(term in subject_and_body for term in _CONFIRMATION_TERMS)
        and _fold(candidate["company"]) in company_context
        and _sender_matches(message["sender"], candidate["canonical_url"])
    )


def _looks_like_confirmation_summary(thread: dict[str, Any]) -> bool:
    summary_text = _fold(
        f"{thread.get('subject', '')}\n{thread.get('snippet', '')}"
    )
    return any(term in summary_text for term in _CONFIRMATION_TERMS)


def _uncertain_candidates(ledger: Ledger) -> list[dict[str, str]]:
    rows = ledger.connection.execute(
        """
        SELECT
          applications.id AS application_id,
          applications.company,
          applications.title,
          applications.canonical_url,
          submit_intents.intent_id
        FROM applications
        JOIN submit_intents
          ON submit_intents.application_id = applications.id
        WHERE applications.current_state = 'submit_unknown'
          AND submit_intents.status = 'submit_unknown'
        ORDER BY applications.created_at, applications.id
        """
    ).fetchall()
    return [
        {key: str(row[key]) for key in row.keys()}
        for row in rows
    ]


def _evidence_sha256(
    message: dict[str, str], candidate: dict[str, str]
) -> str:
    encoded = json.dumps(
        {
            "message": message,
            "application_id": candidate["application_id"],
            "intent_id": candidate["intent_id"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def reconcile_confirmation_threads(
    *,
    ledger_path: Path,
    threads: list[dict[str, Any]],
    thread_loader: Callable[[str], dict[str, Any]],
    seen_state: Path,
    expected_recipient: str | None = None,
) -> dict[str, Any]:
    ledger = Ledger(ledger_path)
    reconciled: list[dict[str, str]] = []
    blocked: list[dict[str, str]] = []
    checked_threads = 0
    seen_messages_after_success: list[str] = []
    try:
        for thread in threads:
            thread_id = str(thread.get("id") or "")
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]+", thread_id)
                or not _looks_like_confirmation_summary(thread)
            ):
                continue
            checked_threads += 1
            payload = thread_loader(thread_id)
            messages = (
                payload.get("thread", {}).get("messages", [])
                if isinstance(payload, dict)
                else []
            )
            if not isinstance(messages, list):
                messages = []
            for raw_message in messages:
                if not isinstance(raw_message, dict):
                    continue
                message = _message_from_payload(raw_message)
                if message is None or message["thread_id"] != thread_id:
                    continue
                received_epoch = datetime.fromisoformat(
                    message["received_at"]
                ).timestamp()
                if message_is_seen(
                    seen_state,
                    message_id=message["message_id"],
                    thread_id=thread_id,
                    received_epoch=received_epoch,
                ):
                    continue
                uncertain = _uncertain_candidates(ledger)
                candidates = [
                    candidate
                    for candidate in uncertain
                    if _confirmation_matches(message, candidate, expected_recipient)
                ]
                if not candidates:
                    candidates = [
                        candidate
                        for candidate in uncertain
                        if _confirmation_matches_without_title(
                            message, candidate, expected_recipient
                        )
                    ]
                if len(candidates) != 1:
                    reason = (
                        "ambiguous_application_match"
                        if len(candidates) > 1
                        else "no_exact_uncertain_application"
                    )
                    blocked.append(
                        {
                            "message_id": message["message_id"],
                            "thread_id": thread_id,
                            "status": reason,
                        }
                    )
                    continue
                candidate = candidates[0]
                try:
                    status = ledger.reconcile_submission_confirmation(
                        intent_id=candidate["intent_id"],
                        message_id=message["message_id"],
                        thread_id=thread_id,
                        evidence_sha256=_evidence_sha256(message, candidate),
                        received_at=message["received_at"],
                    )
                except FenceError:
                    blocked.append(
                        {
                            "message_id": message["message_id"],
                            "thread_id": thread_id,
                            "status": "ledger_fence_blocked",
                        }
                    )
                    continue
                reconciled.append(
                    {
                        "application_id": candidate["application_id"],
                        "message_id": message["message_id"],
                        "thread_id": thread_id,
                        "status": status,
                    }
                )
                seen_messages_after_success.append(message["message_id"])
    finally:
        ledger.close()
    if seen_messages_after_success:
        mark_messages_seen(seen_state, seen_messages_after_success)
    return {
        "version": 1,
        "checked_threads": checked_threads,
        "reconciled": reconciled,
        "blocked": blocked,
    }


def _gmail_confirmation_threads(account: str, executable: str) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            executable,
            "gmail",
            "search",
            "--account",
            account,
            "--json",
            "--limit",
            "100",
            (
                'newer_than:30d ("application received" OR '
                '"received your application" OR '
                '"thank you for applying" OR "thanks for applying" OR '
                '"thank you for your online submission" OR '
                '"thank you for your interest" OR "has been received" OR '
                '"got your application" OR '
                '"応募が完了" OR "応募を受け付け" OR "ご応募いただ" OR '
                '"ご応募ありがとう")'
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    value = json.loads(completed.stdout)
    threads = value.get("threads", [])
    return [row for row in threads if isinstance(row, dict)]


def _gmail_thread(
    account: str, thread_id: str, executable: str
) -> dict[str, Any]:
    completed = subprocess.run(
        [
            executable,
            "gmail",
            "thread",
            "get",
            "--account",
            account,
            "--json",
            "--wrap-untrusted",
            "--full",
            "--sanitize-content",
            thread_id,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise ValueError("gog Gmail thread result must be an object")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("reconcile",))
    parser.add_argument("--account", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--seen", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--gog",
        default=os.environ.get("JOB_SEARCH_GOG", "gog"),
    )
    args = parser.parse_args()

    result = reconcile_confirmation_threads(
        ledger_path=args.ledger,
        threads=_gmail_confirmation_threads(args.account, args.gog),
        thread_loader=lambda thread_id: _gmail_thread(
            args.account, thread_id, args.gog
        ),
        seen_state=args.seen,
        expected_recipient=args.account,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(args.output, 0o600)


if __name__ == "__main__":
    main()
