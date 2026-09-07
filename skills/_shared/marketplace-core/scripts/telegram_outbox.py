"""Small durable, at-most-once outbox for Telegram receipt messages."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Optional


class OutboxError(RuntimeError):
    """Base error for outbox state or storage failures."""


class IdempotencyConflict(OutboxError):
    """The event key already names a different message."""


class StaleClaim(OutboxError):
    """The claim this worker holds was reclaimed and re-issued, so its result is not authoritative.

    A resolver must still match the claim identity it was handed. This protects explicit pre-send
    retries and any future ownership transfer. Abandoned claims are quarantined as
    delivery_uncertain and are never transferred to another sender.
    """


class InvalidState(OutboxError):
    """An operation was requested for an incompatible outbox state."""


@dataclass(frozen=True)
class OutboxItem:
    event_key: str
    message_sha256: str
    message: str
    status: str
    attempt_count: int
    provider_message_id: Optional[str]
    created_at: str
    claimed_at: Optional[str]
    delivered_at: Optional[str]
    last_error_code: Optional[str]


_TABLE = "telegram_outbox"
_COMMON_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    event_key TEXT PRIMARY KEY,
    message_sha256 TEXT NOT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'sending', 'delivered', 'delivery_uncertain'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    delivered_at TEXT,
    last_error_code TEXT
)
"""


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _message_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _owner_only_permissions(database: Path) -> None:
    database.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    database.parent.chmod(0o700)
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = database if not suffix else database.with_name(database.name + suffix)
        try:
            path.chmod(0o600)
        except FileNotFoundError:
            continue


@contextmanager
def _connection(database: Path) -> Iterator[sqlite3.Connection]:
    path = Path(database)
    _owner_only_permissions(path)
    connection = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(_SCHEMA)
        _owner_only_permissions(path)
        yield connection
    finally:
        _owner_only_permissions(path)
        connection.close()


@contextmanager
def _write_connection(database: Path) -> Iterator[sqlite3.Connection]:
    with _connection(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _item_from_row(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        event_key=str(row["event_key"]),
        message_sha256=str(row["message_sha256"]),
        message=str(row["message"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        provider_message_id=(
            None
            if row["provider_message_id"] is None
            else str(row["provider_message_id"])
        ),
        created_at=str(row["created_at"]),
        claimed_at=(None if row["claimed_at"] is None else str(row["claimed_at"])),
        delivered_at=(
            None if row["delivered_at"] is None else str(row["delivered_at"])
        ),
        last_error_code=(
            None
            if row["last_error_code"] is None
            else str(row["last_error_code"])
        ),
    )


def enqueue(database: Path, event_key: str, message: str, created_at: str) -> bool:
    """Insert one pending message, returning False for an exact replay."""

    event_key = _require_text("event_key", event_key)
    message = _require_text("message", message)
    created_at = _require_text("created_at", created_at)
    message_sha256 = _message_hash(message)

    with _write_connection(database) as connection:
        existing = connection.execute(
            f"SELECT message_sha256 FROM {_TABLE} WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        if existing is not None:
            if existing["message_sha256"] != message_sha256:
                raise IdempotencyConflict(event_key)
            return False
        connection.execute(
            f"""
            INSERT INTO {_TABLE} (
                event_key, message_sha256, message, status, attempt_count,
                provider_message_id, created_at, claimed_at, delivered_at,
                last_error_code
            ) VALUES (?, ?, ?, 'pending', 0, NULL, ?, NULL, NULL, NULL)
            """,
            (event_key, message_sha256, message, created_at),
        )
        return True


def claim_next(database: Path) -> Optional[OutboxItem]:
    """Atomically claim the oldest pending message for sending."""

    with _write_connection(database) as connection:
        row = connection.execute(
            f"""
            SELECT * FROM {_TABLE}
            WHERE status = 'pending'
            ORDER BY created_at ASC, event_key ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        claimed_at = _utc_now()
        connection.execute(
            f"""
            UPDATE {_TABLE}
            SET status = 'sending',
                attempt_count = attempt_count + 1,
                claimed_at = ?
            WHERE event_key = ? AND status = 'pending'
            """,
            (claimed_at, row["event_key"]),
        )
        claimed = connection.execute(
            f"SELECT * FROM {_TABLE} WHERE event_key = ?",
            (row["event_key"],),
        ).fetchone()
        assert claimed is not None
        return _item_from_row(claimed)


def _get_item(connection: sqlite3.Connection, event_key: str) -> sqlite3.Row:
    row = connection.execute(
        f"SELECT * FROM {_TABLE} WHERE event_key = ?", (event_key,)
    ).fetchone()
    if row is None:
        raise KeyError(event_key)
    return row


def _fenced(row: sqlite3.Row, claimed_at: Optional[str]) -> sqlite3.Row:
    """Reject a resolver whose claim no longer matches the row's.

    claimed_at is the claim identity claim_next handed out. It is optional so existing callers keep
    working unchanged; deliver_pending passes it, which is the loop every marketplace lane uses.
    """
    if claimed_at is not None and row["claimed_at"] != claimed_at:
        raise StaleClaim(row["event_key"])
    return row


def mark_delivered(
    database: Path,
    event_key: str,
    provider_message_id: str,
    delivered_at: str,
    *,
    claimed_at: Optional[str] = None,
) -> None:
    """Record a provider acknowledgement without allowing a downgrade."""

    event_key = _require_text("event_key", event_key)
    provider_message_id = _require_text(
        "provider_message_id", provider_message_id
    )
    delivered_at = _require_text("delivered_at", delivered_at)
    with _write_connection(database) as connection:
        row = _fenced(_get_item(connection, event_key), claimed_at)
        if row["status"] == "delivered":
            if row["provider_message_id"] != provider_message_id:
                raise IdempotencyConflict(event_key)
            return
        if row["status"] not in {"sending", "delivery_uncertain"}:
            raise InvalidState(row["status"])
        try:
            connection.execute(
                f"""
                UPDATE {_TABLE}
                SET status = 'delivered',
                    provider_message_id = ?,
                    delivered_at = ?,
                    last_error_code = NULL
                WHERE event_key = ?
                """,
                (provider_message_id, delivered_at, event_key),
            )
        except sqlite3.IntegrityError as error:
            raise IdempotencyConflict(provider_message_id) from error


def mark_pre_send_failed(
    database: Path, event_key: str, error_code: str, *, claimed_at: Optional[str] = None
) -> None:
    """Return a claim to pending only when no provider call was attempted."""

    event_key = _require_text("event_key", event_key)
    error_code = _require_text("error_code", error_code)
    with _write_connection(database) as connection:
        row = _fenced(_get_item(connection, event_key), claimed_at)
        if row["status"] != "sending":
            if row["status"] == "pending":
                return
            raise InvalidState(row["status"])
        connection.execute(
            f"""
            UPDATE {_TABLE}
            SET status = 'pending', claimed_at = NULL, last_error_code = ?
            WHERE event_key = ? AND status = 'sending'
            """,
            (error_code, event_key),
        )


def mark_delivery_uncertain(
    database: Path, event_key: str, error_code: str, *, claimed_at: Optional[str] = None
) -> None:
    """Quarantine a message once the provider call may have happened."""

    event_key = _require_text("event_key", event_key)
    error_code = _require_text("error_code", error_code)
    with _write_connection(database) as connection:
        row = _fenced(_get_item(connection, event_key), claimed_at)
        if row["status"] == "delivery_uncertain":
            return
        if row["status"] != "sending":
            raise InvalidState(row["status"])
        connection.execute(
            f"""
            UPDATE {_TABLE}
            SET status = 'delivery_uncertain', last_error_code = ?
            WHERE event_key = ? AND status = 'sending'
            """,
            (error_code, event_key),
        )


def reclaim_stale(database: Path, *, older_than_seconds: int = 900) -> int:
    """Quarantine claims abandoned after a provider call may have started.

    A claim moves to 'sending' before the provider call and is resolved after it. If the process
    dies in between, the message may already have reached Telegram. Returning it to pending would
    blindly resend it. Keep its claim evidence and move it to delivery_uncertain instead; provider
    readback may later reconcile it with mark_delivered.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(1, int(older_than_seconds)))).isoformat()
    with _write_connection(database) as connection:
        cursor = connection.execute(
            f"""
            UPDATE {_TABLE}
            SET status = 'delivery_uncertain', last_error_code = 'sender_abandoned'
            WHERE status = 'sending' AND provider_message_id IS NULL AND claimed_at IS NOT NULL
              AND claimed_at < ?
            """,
            (cutoff,),
        )
        return int(cursor.rowcount or 0)


def to_common_outbox(
    item: OutboxItem,
    *,
    loop_id: str,
    tenant_id: Optional[str] = None,
    job_id: Optional[str] = None,
    effect_id: Optional[str] = None,
) -> dict[str, object]:
    """Project the existing SQLite row to the common OutboxItem wire contract."""

    loop_id = _require_text("loop_id", loop_id)
    if not _COMMON_ID.fullmatch(loop_id):
        raise ValueError("loop_id is not a common ID")
    for name, value in (("tenant_id", tenant_id), ("job_id", job_id), ("effect_id", effect_id)):
        if value is not None and (not isinstance(value, str) or not _COMMON_ID.fullmatch(value)):
            raise ValueError(f"{name} is not a common ID")
    if not 1 <= len(item.event_key) <= 1024:
        raise ValueError("event_key is outside the common message_key bounds")
    if not _SHA256.fullmatch(item.message_sha256):
        raise ValueError("message_sha256 is invalid")
    if item.status not in {"pending", "sending", "delivered", "delivery_uncertain"}:
        raise ValueError("outbox status is invalid")
    if item.attempt_count < 0:
        raise ValueError("outbox attempt_count is invalid")

    def timestamp(name: str, value: Optional[str], *, required: bool = False) -> None:
        if value is None:
            if required:
                raise ValueError(f"{name} is invalid")
            return
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError(f"{name} is invalid") from error
        if parsed.utcoffset() is None:
            raise ValueError(f"{name} is invalid")

    timestamp("created_at", item.created_at, required=True)
    timestamp("claimed_at", item.claimed_at)
    timestamp("delivered_at", item.delivered_at)
    if item.status == "pending" and (
        item.claimed_at is not None or item.delivered_at is not None or item.provider_message_id is not None
    ):
        raise ValueError("pending outbox evidence is invalid")
    if item.status in {"sending", "delivery_uncertain"} and (
        item.attempt_count < 1 or item.claimed_at is None
        or item.delivered_at is not None or item.provider_message_id is not None
    ):
        raise ValueError(f"{item.status} outbox evidence is invalid")
    if item.status == "delivered" and (
        item.attempt_count < 1 or item.claimed_at is None
        or item.delivered_at is None or not item.provider_message_id
    ):
        raise ValueError("delivered outbox evidence is invalid")
    provider_ids = [] if item.provider_message_id is None else [item.provider_message_id]
    return {
        "schema_version": 1,
        "record_type": "outbox_item",
        "message_key": item.event_key,
        "tenant_id": tenant_id,
        "job_id": job_id,
        "effect_id": effect_id,
        "loop_id": loop_id,
        "status": item.status,
        "attempt_count": item.attempt_count,
        "payload_sha256": item.message_sha256,
        "provider": "telegram",
        "created_at": item.created_at,
        "claimed_at": item.claimed_at,
        "delivered_at": item.delivered_at,
        "provider_message_ids": provider_ids,
    }


def list_items(database: Path) -> list[OutboxItem]:
    """Return all outbox rows in creation order."""

    with _connection(database) as connection:
        rows = connection.execute(
            f"SELECT * FROM {_TABLE} ORDER BY created_at ASC, event_key ASC"
        ).fetchall()
        return [_item_from_row(row) for row in rows]


__all__ = [
    "IdempotencyConflict",
    "InvalidState",
    "OutboxError",
    "OutboxItem",
    "StaleClaim",
    "claim_next",
    "enqueue",
    "list_items",
    "mark_delivered",
    "mark_delivery_uncertain",
    "mark_pre_send_failed",
    "reclaim_stale",
    "to_common_outbox",
]
