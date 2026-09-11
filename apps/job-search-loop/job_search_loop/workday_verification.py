from __future__ import annotations

import hashlib
import json
import html
import os
import re
import sqlite3
import subprocess
import uuid
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlsplit

from .browser_agent.workday_account import MachineWorkdayCredentialStore
from .workday_credentials import known_tenants as legacy_known_tenants


class VerificationError(RuntimeError):
    pass


MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
DEFAULT_CLAIM_LEASE_SECONDS = 900


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claim_time(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise VerificationError("claim time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _stored_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise VerificationError("stored Workday claim time is invalid") from error
    if parsed.tzinfo is None:
        raise VerificationError("stored Workday claim time lacks timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class VerificationTarget:
    message_id: str
    tenant: str
    verification_url: str
    url_sha256: str
    kind: str = "activation"

    @property
    def event_key(self) -> str:
        material = f"{self.message_id}\n{self.tenant}\n{self.url_sha256}"
        if self.kind != "activation":
            material += f"\n{self.kind}"
        return "workday-verify:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def receipt(self, status: str) -> dict[str, str]:
        return {
            "message_id": self.message_id,
            "tenant": self.tenant,
            "url_sha256": self.url_sha256,
            "status": status,
        }


def _verification_language_matches(subject: str, body: str) -> bool:
    text = f"{subject}\n{body}".casefold()
    return (
        "verify your candidate account" in text
        or "reset your password for your candidate account" in text
        or (
            "confirm your email address" in text
            and "candidate account" in text
        )
        or ("候補者アカウント" in text and "メールアドレス" in text)
    )


def _candidate_urls(body: str) -> list[str]:
    decoded = html.unescape(body)
    return [
        value.rstrip(".,;:!?)")
        for value in URL_PATTERN.findall(decoded)
    ]


def _valid_activation_url(
    value: str, tenants: set[str]
) -> tuple[str, str, str] | None:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or host not in tenants
    ):
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    lowered = [segment.casefold() for segment in segments]
    for marker, kind in (("activate", "activation"), ("passwordreset", "password_reset")):
        if marker not in lowered:
            continue
        marker_index = lowered.index(marker)
        if marker_index + 1 < len(segments) and segments[marker_index + 1]:
            return host, value, kind
    return None


def extract_verification_target(
    *,
    message_id: str,
    subject: str,
    sender: str,
    body: str,
    credential_store: Path,
) -> VerificationTarget:
    if not MESSAGE_ID_PATTERN.fullmatch(message_id):
        raise VerificationError("Gmail message ID is invalid")
    sender_address = parseaddr(sender)[1].casefold()
    sender_domain = sender_address.rpartition("@")[2]
    if sender_domain not in {"myworkday.com", "otp.workday.com"}:
        raise VerificationError("Workday verification sender is not trusted")
    if not _verification_language_matches(subject, body):
        raise VerificationError("message is not a Workday account verification")
    raw_store = json.loads(credential_store.read_text(encoding="utf-8"))
    tenants = set(
        MachineWorkdayCredentialStore(credential_store).known_tenants()
        if isinstance(raw_store, dict) and isinstance(raw_store.get("credentials"), list)
        else legacy_known_tenants(credential_store)
    )
    matches = {
        match
        for candidate in _candidate_urls(body)
        if (match := _valid_activation_url(candidate, tenants)) is not None
    }
    if len(matches) != 1:
        raise VerificationError(
            "message must contain exactly one known-tenant activation URL"
        )
    tenant, verification_url, kind = matches.pop()
    return VerificationTarget(
        message_id=message_id,
        tenant=tenant,
        verification_url=verification_url,
        url_sha256=hashlib.sha256(verification_url.encode("utf-8")).hexdigest(),
        kind=kind,
    )


def _decoded_html_parts(value: object) -> list[str]:
    parts: list[str] = []
    if isinstance(value, dict):
        if value.get("mimeType") == "text/html":
            encoded = (value.get("body") or {}).get("data")
            if isinstance(encoded, str):
                padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
                parts.append(base64.urlsafe_b64decode(padded).decode("utf-8", "replace"))
        for child in value.values():
            parts.extend(_decoded_html_parts(child))
    elif isinstance(value, list):
        for child in value:
            parts.extend(_decoded_html_parts(child))
    return parts


def extract_verification_target_from_gmail(
    *,
    account: str,
    thread_id: str,
    message_id: str,
    credential_store: Path,
    gog: str = "gog",
) -> VerificationTarget:
    completed = subprocess.run(
        [
            gog,
            "gmail",
            "thread",
            "get",
            "--account",
            account,
            "--json",
            "--full",
            "--gmail-no-send",
            thread_id,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    value = json.loads(completed.stdout)
    messages = ((value.get("thread") or {}).get("messages") or [])
    matches = [row for row in messages if isinstance(row, dict) and row.get("id") == message_id]
    if len(matches) != 1:
        raise VerificationError("Gmail result must contain the exact message once")
    message = matches[0]
    headers = {
        str(row.get("name") or "").casefold(): str(row.get("value") or "")
        for row in ((message.get("payload") or {}).get("headers") or [])
        if isinstance(row, dict)
    }
    bodies = _decoded_html_parts(message.get("payload"))
    if len(bodies) != 1:
        raise VerificationError("Workday message must contain exactly one HTML body")
    return extract_verification_target(
        message_id=message_id,
        subject=headers.get("subject", ""),
        sender=headers.get("from", ""),
        body=bodies[0],
        credential_store=credential_store,
    )


class VerificationStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.connection = sqlite3.connect(
            self.path, timeout=10, isolation_level=None
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS workday_verifications (
              event_key TEXT PRIMARY KEY,
              message_id TEXT NOT NULL,
              tenant TEXT NOT NULL,
              url_sha256 TEXT NOT NULL,
              status TEXT NOT NULL,
              fence TEXT,
              created_at TEXT NOT NULL,
              claimed_at TEXT,
              completed_at TEXT
            )
            """
        )
        columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(workday_verifications)"
            )
        }
        if "claimed_at" not in columns:
            self.connection.execute(
                "ALTER TABLE workday_verifications ADD COLUMN claimed_at TEXT"
            )
        os.chmod(self.path, 0o600)

    def close(self) -> None:
        self.connection.close()

    def claim(
        self,
        target: VerificationTarget,
        *,
        now: datetime | None = None,
        lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> str | None:
        if lease_seconds <= 0:
            raise VerificationError("claim lease must be positive")
        claimed_at = _claim_time(now)
        claimed_at_text = claimed_at.isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO workday_verifications
                  (event_key,message_id,tenant,url_sha256,status,created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    target.event_key,
                    target.message_id,
                    target.tenant,
                    target.url_sha256,
                    claimed_at_text,
                ),
            )
            row = self.connection.execute(
                """
                SELECT status,fence,created_at,claimed_at
                FROM workday_verifications WHERE event_key = ?
                """,
                (target.event_key,),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            status, old_fence, created_at, prior_claimed_at = row
            if status == "claimed":
                lease_anchor = _stored_time(prior_claimed_at or created_at)
                if claimed_at - lease_anchor < timedelta(seconds=lease_seconds):
                    self.connection.commit()
                    return None
                fence = uuid.uuid4().hex
                changed = self.connection.execute(
                    """
                    UPDATE workday_verifications
                    SET fence=?,claimed_at=?,completed_at=NULL
                    WHERE event_key=? AND status='claimed' AND fence=?
                    """,
                    (fence, claimed_at_text, target.event_key, old_fence),
                ).rowcount
                self.connection.commit()
                return fence if changed == 1 else None
            if status != "pending":
                self.connection.commit()
                return None
            fence = uuid.uuid4().hex
            changed = self.connection.execute(
                """
                UPDATE workday_verifications
                SET status='claimed',fence=?,claimed_at=?
                WHERE event_key=? AND status='pending'
                """,
                (fence, claimed_at_text, target.event_key),
            ).rowcount
            self.connection.commit()
            return fence if changed == 1 else None
        except BaseException:
            self.connection.rollback()
            raise

    def _transition(
        self, event_key: str, fence: str, from_state: str, to_state: str
    ) -> None:
        completed_at = _now() if to_state in {"opened", "navigation_unknown"} else None
        changed = self.connection.execute(
            """
            UPDATE workday_verifications
            SET status=?, completed_at=?
            WHERE event_key=? AND fence=? AND status=?
            """,
            (to_state, completed_at, event_key, fence, from_state),
        ).rowcount
        if changed != 1:
            raise VerificationError("Workday verification fence mismatch")

    def mark_navigation_started(self, event_key: str, fence: str) -> None:
        self._transition(event_key, fence, "claimed", "navigation_started")

    def release_claim(self, event_key: str, fence: str) -> None:
        changed = self.connection.execute(
            """
            UPDATE workday_verifications
            SET status='pending',fence=NULL,claimed_at=NULL
            WHERE event_key=? AND fence=? AND status='claimed'
            """,
            (event_key, fence),
        ).rowcount
        if changed != 1:
            raise VerificationError("Workday verification claim release mismatch")

    def mark_opened(self, event_key: str, fence: str) -> None:
        self._transition(event_key, fence, "navigation_started", "opened")

    def mark_unknown(self, event_key: str, fence: str) -> None:
        self._transition(
            event_key, fence, "navigation_started", "navigation_unknown"
        )

    def status(self, event_key: str) -> str:
        row = self.connection.execute(
            "SELECT status FROM workday_verifications WHERE event_key=?",
            (event_key,),
        ).fetchone()
        if row is None:
            raise KeyError(event_key)
        return str(row[0])
