#!/usr/bin/env python3
"""checkpoint_via_tg.py -- ask Dais one yes/no question over Telegram and let a LATER
gig pass see whether he answered, without ever making the pass that hit the block wait.

  send_checkpoint()          enqueue+send one question, write a pending state file
  poll_checkpoint_reply()    a LATER pass calls this; never blocks, never waits

★ HARD RULE -- who may consume this primitive (Dais 2026-08-09) ★

Allowed consumer classes, exclusively:
  1. money-moving      (funds leave an account; refunds; price changes on live orders)
  2. irreversible      (a broadcast/deletion/submission that cannot be undone)
  3. ToS-risk          (an action that could get the marketplace account sanctioned)

QUALITY ISSUES ARE NEVER A HUMAN QUESTION. A low predelivery score, a weak draft, a
"good enough?" doubt -- the loop's answer to bad quality is to REBUILD the deliverable,
not to ask a human to bless it. An earlier revision of this file wired a
predelivery_score_low consumer; Dais rejected that design explicitly. Do not re-add it,
and do not add any consumer whose question reduces to "is this output good enough".

NO CONSUMER SHIPS IN THIS SLICE. This file is the primitive only; the first real
consumer must name which of the three classes above it belongs to, in its own review.

Send reuses the production Telegram path this repo already runs daily
(telegram_outbox.TelegramOutbox + the repository-owned shared Bot API client) --
no new bot token and no second send implementation.

Reading is the harder half. Three read paths were tried by earlier work in this repo and
each was a real wall, not a naming problem (see telegram_web_read.py's docstring):
``openclaw message read`` refuses (the gateway itself owns getUpdates on that token) and
``getUpdates`` conflicts with Life Manager's webhook on the other token. The one path that
works -- reading the already-logged-in Telegram Web tab over CDP -- is a SHARED browser
(``interactive:dais`` in ~/.config/ai/registry/browsers.toml, owner=main-session) gated by
browser-guard.sh precisely because two uncoordinated leases on it already broke a session
once (2026-07-26/27). So ``poll_checkpoint_reply`` takes the reader as an injected callable
(``read_bubbles: () -> list[{"from","text"}]``), shaped EXACTLY like
``telegram_web_read.parse_bubbles``'s output. Wiring the real CDP reader later is one
adapter function; no browser lease is taken by this module itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telegram_outbox  # noqa: E402  -- the ONE durable send queue, reused as-is

DEFAULT_GIG_DIR = Path.home() / "gig"
DEFAULT_TARGET = os.environ.get("GIG_REPORT_CHAT", "")  # set by launchd job; empty = no report
CHECKPOINT_DIR_NAME = "checkpoints"

# A checkpoint question with an empty or placeholder decision context is exactly the
# message Dais cannot act on -- the live wiring-test send on 2026-08-08 arrived with
# 不明点/project unfilled and proved it. Fail closed on these, don't send them.
PLACEHOLDER_VALUES = frozenset({
    "不明", "不明点", "unknown", "n/a", "na", "tbd", "todo", "placeholder", "-", "?",
})

# Every checkpoint must carry enough context to decide from the phone, without
# opening the Mac: what the buyer asked for, what artifact this is about, and
# what the loop itself recommends.
REQUIRED_CONTEXT_FIELDS = ("request_summary", "deliverable_ref", "recommendation")


def _checkpoint_path(gig_dir: Path, checkpoint_id: str) -> Path:
    safe = checkpoint_id.replace("/", "_").replace("\x00", "")
    return Path(gig_dir) / CHECKPOINT_DIR_NAME / f"{safe}.json"


def _read_checkpoint(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_checkpoint(path: Path, record: dict[str, Any]) -> None:
    """Same atomic-write shape as telegram_report._write_work_event_report_state:
    tempfile in the same directory, fsync, then os.replace -- a reader (poll, or a
    human tailing the file) must never see a half-written row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _validated_decision_context(context: Any) -> dict[str, str]:
    """The three fields a human needs to answer from the phone -- or refuse to send.

    ★ FAIL-CLOSED. ★ Empty, whitespace, or a recognizable placeholder ("不明",
    "TBD", ...) in any required field raises: a question without its context is
    noise Dais cannot act on, and noise trains him to stop reading (the exact
    failure telegram_outbox.SUPPRESS_WINDOW_SECONDS documents).
    """
    if not isinstance(context, dict):
        raise ValueError("checkpoint context must be a dict with decision fields")
    validated: dict[str, str] = {}
    for field in REQUIRED_CONTEXT_FIELDS:
        value = str(context.get(field) or "").strip()
        if not value or value.lower() in PLACEHOLDER_VALUES or value in PLACEHOLDER_VALUES:
            raise ValueError(f"checkpoint context field is empty or placeholder: {field}")
        validated[field] = value
    return validated


def send_checkpoint(
    *,
    gig_dir: Path,
    checkpoint_id: str,
    question: str,
    options: list[str],
    context: dict[str, Any],
    outbox: telegram_outbox.TelegramOutbox,
    transport: Callable[[str], str],
    now_epoch: int,
) -> dict[str, Any]:
    """Ask once, durably. A repeat call for the same checkpoint_id is a no-op:
    the outbox's UNIQUE(event_key) already dedupes the SEND, but only the state
    file on disk can stop a second call from overwriting Dais's already-recorded
    answer back to "pending" -- so this checks the file first, not the outbox.
    """
    checkpoint_id = str(checkpoint_id or "").strip()
    question = str(question or "").strip()
    options = [str(option).strip() for option in (options or []) if str(option).strip()]
    if not checkpoint_id or not question or not options:
        raise ValueError("checkpoint requires checkpoint_id, question, and options")
    decision_context = _validated_decision_context(context)

    path = _checkpoint_path(Path(gig_dir), checkpoint_id)
    existing = _read_checkpoint(path)
    if existing is not None:
        return existing

    extra = {
        key: value for key, value in (context or {}).items()
        if key not in REQUIRED_CONTEXT_FIELDS
    }
    message = "\n".join((
        f"❓ {question}",
        f"依頼内容: {decision_context['request_summary']}",
        f"対象: {decision_context['deliverable_ref']}",
        f"ループの推奨: {decision_context['recommendation']}",
        f"選択肢: {' / '.join(options)}",
        f"(checkpoint_id={checkpoint_id})",
        "Telegramでそのまま返信してください。",
    ))
    event_key = f"gig:telegram:checkpoint:v1:{checkpoint_id}"
    outbox.enqueue(event_key=event_key, kind="checkpoint", message=message, created_at=now_epoch)
    result = telegram_outbox.dispatch_one(
        outbox, owner=f"gig-checkpoint-{checkpoint_id}", now=lambda: now_epoch, transport=transport,
    )

    record = {
        "version": 1,
        "checkpoint_id": checkpoint_id,
        "question": question,
        "options": options,
        "context": {**decision_context, **extra},
        "status": "pending",
        "asked_at": now_epoch,
        "send_status": result["status"],
        "message_id": result.get("message_id"),
        "reply_text": None,
        "matched_option": None,
        "answered_at": None,
    }
    _write_checkpoint(path, record)
    return record


def poll_checkpoint_reply(
    *,
    gig_dir: Path,
    checkpoint_id: str,
    read_bubbles: Callable[[], list[dict[str, Any]]],
    owner: str = "dais",
    now_epoch: int | None = None,
) -> dict[str, Any] | None:
    """Called by a LATER pass. Never blocks: one read of whatever the injected
    reader hands back, matched against this checkpoint's own options, nothing more.

    Bubbles carry no per-message timestamp (telegram_web_read.py strips the clock on
    purpose). So only the SINGLE NEWEST bubble in the fetched window is trusted as
    "the reply" -- in a one-question chat that is exactly the next thing Dais typed;
    anything older is not this checkpoint's answer even if it happens to contain an
    option word.
    """
    path = _checkpoint_path(Path(gig_dir), str(checkpoint_id))
    record = _read_checkpoint(path)
    if record is None:
        return None
    if record.get("status") != "pending":
        return record

    bubbles = read_bubbles() or []
    if not bubbles:
        return record
    newest = bubbles[-1]
    if not isinstance(newest, dict) or newest.get("from") != owner:
        return record
    text = str(newest.get("text") or "")
    options = record.get("options") or []
    matched = next((option for option in options if option and option in text), None)
    if matched is None:
        return record

    record = {
        **record,
        "status": "answered",
        "reply_text": text,
        "matched_option": matched,
        "answered_at": now_epoch if now_epoch is not None else int(time.time()),
    }
    _write_checkpoint(path, record)
    return record


def _main(argv: list[str] | None = None) -> int:
    import telegram_report  # deferred: only the CLI's send path needs the real transport

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("send", "poll"))
    parser.add_argument("--gig-dir", type=Path, default=DEFAULT_GIG_DIR)
    parser.add_argument("--checkpoint-id", required=True)
    parser.add_argument("--question")
    parser.add_argument("--options", help="comma-separated, e.g. 'はい,やり直し'")
    parser.add_argument(
        "--context-json", default="{}",
        help="must include request_summary, deliverable_ref, recommendation (fail-closed)",
    )
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument(
        "--bubbles-json", type=Path,
        help="poll only: a JSON file of [{\"from\":..,\"text\":..}, ...] from the real "
             "Telegram reader; without this, poll reports 'still pending' every time.",
    )
    args = parser.parse_args(argv)
    now_epoch = int(time.time())

    if args.command == "send":
        if not args.question or not args.options:
            raise SystemExit("send requires --question and --options")
        outbox = telegram_outbox.TelegramOutbox(args.gig_dir / "telegram-outbox.sqlite3")
        transport = telegram_report.OpenClawTelegramTransport(
            target=args.target, receipt_dir=args.gig_dir / "telegram-delivery-receipts",
        )
        record = send_checkpoint(
            gig_dir=args.gig_dir,
            checkpoint_id=args.checkpoint_id,
            question=args.question,
            options=[option.strip() for option in args.options.split(",")],
            context=json.loads(args.context_json),
            outbox=outbox,
            transport=transport,
            now_epoch=now_epoch,
        )
    else:
        bubbles: list[dict[str, Any]] = []
        if args.bubbles_json is not None:
            bubbles = json.loads(args.bubbles_json.read_text(encoding="utf-8"))
        record = poll_checkpoint_reply(
            gig_dir=args.gig_dir,
            checkpoint_id=args.checkpoint_id,
            read_bubbles=lambda: bubbles,
            now_epoch=now_epoch,
        )
        if record is None:
            print(json.dumps({"ok": False, "error": "unknown checkpoint_id"}))
            return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
