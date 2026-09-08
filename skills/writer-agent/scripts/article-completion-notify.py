#!/usr/bin/env python3
"""Durable, idempotent article completion receipt."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from publication_resume import (
    ACTIVE_PAIRS,
    LEGACY_EXACT8_PAIRS,
    PublicationStore,
)  # noqa: E402
from publication_contract_resolver import (  # noqa: E402
    PublicationContractError,
    resolve_publication_contract,
)
from writer_report_worker import telegram_api_transport  # noqa: E402

REQUIRED_PAIRS = ACTIVE_PAIRS
LEGACY_REQUIRED_PAIRS = LEGACY_EXACT8_PAIRS
ACTIVE_EVENT_PREFIX = "article-active-four:"
ACTIVE_ALIAS_EVENT_PREFIX = "article-active-six:"
LEGACY_EVENT_PREFIX = "article-exact8:"


Transport = Callable[[str], str]


@contextmanager
def _outbox_lock(outbox: Path):
    """Serialize one completion event's read/send/write sequence.

    The publication owner normally supplies the outer fence. This narrow
    lock also covers report retries and manual recovery calls so two workers
    cannot both observe a prepared outbox and send the same message.
    """
    lock_path = outbox.with_name(f".{outbox.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def _canonical_event_id(value: Any) -> str:
    event_id = str(value)
    if event_id.startswith(ACTIVE_ALIAS_EVENT_PREFIX):
        return ACTIVE_EVENT_PREFIX + event_id[len(ACTIVE_ALIAS_EVENT_PREFIX):]
    return event_id


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def build_message(event: dict[str, Any]) -> str:
    lines = ["記事の公開が完了しました。", "次の公開先で、公開後の読み戻しを確認しました。"]
    lines.extend(
        f"{item['pair']}：{item['live_url']}"
        for item in event["pairs"]
    )
    lines.append("この報告は公開確認です。実際の入金は別の受取レシートで確認し、未確認の売上は含めません。")
    return "\n".join(lines)


_PAIR_LABELS = {
    "note/ja": "Note（日本語）",
    "substack/ja": "Substack（日本語）",
    "substack/en": "Substack（英語）",
    "x-article/ja": "X記事（日本語）",
    "devto/en": "Dev.to（英語）",
    "zenn-article/ja": "Zenn（日本語）",
    "x-article/en": "X記事（英語）",
    "x-post/ja": "X短文（日本語）",
}


def _pending_event(
    state_path: Path, ledger_path: Path, reason: str,
) -> dict[str, Any] | None:
    store = PublicationStore(state_path, ledger_path)
    if store.completion_status() == "complete":
        return None
    state = store.read()
    try:
        contract = resolve_publication_contract(
            state_path, ledger_path, str(state["run_id"]), state=state
        )
    except (KeyError, PublicationContractError):
        return None
    required = LEGACY_REQUIRED_PAIRS if contract == "legacy-exact8" else REQUIRED_PAIRS
    pairs: list[dict[str, str]] = []
    for pair in required:
        entry = state.get("pairs", {}).get(pair, {})
        status = str(entry.get("status", ""))
        label = _PAIR_LABELS.get(pair, "公開先")
        receipt = entry.get("receipt") if isinstance(entry.get("receipt"), dict) else {}
        live_url = str(receipt.get("live_url", ""))
        if status == "live" and live_url:
            summary = f"公開済み。公開後の読み戻しを確認しました。{live_url}"
        elif status == "intent":
            summary = "公開前の下書きを確認しました。公開はまだ確認できていません。"
        else:
            summary = "公開はまだ確認できていません。外部の公開状態を再確認します。"
        pairs.append({"pair": pair, "label": label, "summary": summary})
    payload = {
        "run_id": str(state.get("run_id", "")),
        "topic_id": str(state.get("topic_id", "")),
        "pairs": pairs,
        "reason": reason or "未完了の公開処理",
        "next_action": "次の自動処理で、未確認の公開先だけを同じ記事のまま再試行します。",
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {
        "version": 1,
        "event_id": f"article-pending:{payload['run_id']}:{digest}",
        **payload,
    }


def build_pending_message(event: dict[str, Any]) -> str:
    lines = ["記事の公開処理を確認しました。"]
    for item in event["pairs"]:
        lines.append(f"{item['label']}：{item['summary']}")
    lines.append(str(event["next_action"]))
    lines.append("未確認の公開や入金を、完了とは数えていません。")
    return "\n".join(lines)


def deliver_pending(
    outbox: Path, event: dict[str, Any], transport: Transport,
) -> dict[str, Any]:
    with _outbox_lock(outbox):
        try:
            current = json.loads(outbox.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict) and current.get("event_id") == event["event_id"]:
            if current.get("status") == "sent" and current.get("message_id"):
                return current
            prepared = current
        else:
            prepared = {
                **event,
                "status": "prepared",
                "prepared_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_write(outbox, prepared)
        message_id = transport(build_pending_message(prepared))
        if not isinstance(message_id, str) or not message_id.strip():
            raise RuntimeError("Telegram transport returned no message ID")
        sent = {
            **prepared,
            "status": "sent",
            "message_id": message_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(outbox, sent)
        return sent


def deliver(
    outbox: Path,
    event: dict[str, Any],
    transport: Transport,
) -> dict[str, Any]:
    with _outbox_lock(outbox):
        event = {**event, "event_id": _canonical_event_id(event["event_id"])}
        try:
            current = json.loads(outbox.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        identity = ("event_id", "run_id", "topic_id", "pairs")
        if isinstance(current, dict):
            current = {
                **current,
                "event_id": _canonical_event_id(current.get("event_id", "")),
            }
            if any(current.get(key) != event.get(key) for key in identity):
                raise ValueError(
                    "completion notification outbox conflicts with completion event"
                )
            if current.get("status") == "sent" and current.get("message_id"):
                return current
            prepared = current
        else:
            prepared = {
                **event,
                "status": "prepared",
                "prepared_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_write(outbox, prepared)
        message_id = transport(build_message(prepared))
        if not isinstance(message_id, str) or not message_id.strip():
            raise RuntimeError("Telegram transport returned no message ID")
        sent = {
            **prepared,
            "status": "sent",
            "message_id": message_id,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(outbox, sent)
        return sent


def _ledger_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def exact8_event(state_path: Path, ledger_path: Path) -> dict[str, Any] | None:
    store = PublicationStore(state_path, ledger_path)
    if store.completion_status() != "complete":
        return None
    state = store.read()
    try:
        contract = resolve_publication_contract(
            state_path, ledger_path, str(state["run_id"]), state=state
        )
    except (KeyError, PublicationContractError):
        return None
    required = LEGACY_REQUIRED_PAIRS if contract == "legacy-exact8" else REQUIRED_PAIRS
    rows = [
        row
        for row in _ledger_rows(ledger_path)
        if row.get("run_id") == state["run_id"]
        and row.get("topic_id") == state["topic_id"]
        and row.get("published") is True
    ]
    by_pair = {
        f"{row.get('platform')}/{row.get('lang')}": row
        for row in rows
    }
    if set(by_pair) != set(required):
        return None
    pairs = [
        {
            "pair": pair,
            "live_url": str(by_pair[pair]["live_url"]),
            "public_id": str(by_pair[pair]["public_id"]),
        }
        for pair in required
    ]
    return {
        "version": 1,
        "event_id": (
            f"{LEGACY_EVENT_PREFIX}{state['run_id']}"
            if required == LEGACY_REQUIRED_PAIRS
            else f"{ACTIVE_EVENT_PREFIX}{state['run_id']}"
        ),
        "run_id": state["run_id"],
        "topic_id": state["topic_id"],
        "pairs": pairs,
    }


def writer_telegram_transport(target: str) -> Transport:
    """Use the Writer-owned Telegram transport, not the gateway CLI.

    The completion owner already has a durable outbox. Reusing the report
    worker's Bot API transport keeps that owner self-contained and avoids a
    gateway reconnect hanging the publication worker after the external
    receipt has been written. The function name remains for compatibility with
    existing callers and fixtures.
    """
    return telegram_api_transport(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument(
        "--target",
        default=os.environ.get("TELEGRAM_TARGET_ID", ""),
    )
    parser.add_argument("--fixture-receipt", help=argparse.SUPPRESS)
    parser.add_argument("--pending", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--reason", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.pending:
            event = _pending_event(args.state, args.ledger, args.reason)
            if event is None:
                print('{"status":"complete"}')
                return 0
            state = PublicationStore(args.state, args.ledger).read()
            outbox = Path(str(state["run_dir"])) / "gates" / "progress-notification.json"
            transport = (
                (lambda _message: str(args.fixture_receipt))
                if args.fixture_receipt
                else writer_telegram_transport(args.target)
            )
            result = deliver_pending(outbox, event, transport)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            return 0
        event = exact8_event(args.state, args.ledger)
        if event is None:
            print('{"status":"pending"}')
            return 0
        state = PublicationStore(args.state, args.ledger).read()
        outbox = (
            Path(str(state["run_dir"]))
            / "gates"
            / "completion-notification.json"
        )
        transport = (
            (lambda _message: str(args.fixture_receipt))
            if args.fixture_receipt
            else writer_telegram_transport(args.target)
        )
        result = deliver(outbox, event, transport)
    except (
        OSError,
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
    ) as error:
        print(f"completion notification pending: {error}", file=sys.stderr)
        return 75
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
