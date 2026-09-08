#!/usr/bin/env python3
"""Durable, idempotent Telegram receipt for the nightly learning cycle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from writer_report_worker import telegram_api_transport


Transport = Callable[[str], str]


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_event(skill_dir: Path, receipt_path: Path) -> dict[str, Any]:
    raw = receipt_path.read_bytes()
    receipt = json.loads(raw)
    run_ids = receipt.get("metric_run_ids")
    if (
        not isinstance(run_ids, list)
        or len(run_ids) != len(set(run_ids))
        or any(
            not isinstance(run_id, str)
            or not run_id.startswith("daily-")
            for run_id in run_ids
        )
    ):
        raise ValueError("self-improve receipt has no unique daily metric run IDs")
    if receipt.get("new_metrics") != len(run_ids):
        raise ValueError("self-improve metric count does not match run IDs")
    observed_at = str(receipt.get("observed_at", ""))
    observed_date = datetime.fromisoformat(observed_at).date().isoformat()
    verification = json.loads(
        (skill_dir / "state/.selfimprove-todo.json").read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(verification.get("missing_count"), int):
        raise ValueError("self-improve verification receipt is invalid")
    weight_hash = receipt.get("after_hash")
    if not isinstance(weight_hash, str) or len(weight_hash) != 64:
        playbook = skill_dir / "reference/learned-playbook.md"
        weight_hash = hashlib.sha256(playbook.read_bytes()).hexdigest()
    learning_status = str(receipt.get("status", ""))
    decision = (
        "keep"
        if learning_status in {"APPLIED", "COMPLETE"}
        else "revert"
        if learning_status == "REVERTED"
        else "no_change"
    )
    reward_status = receipt.get("reward_status")
    if reward_status not in {"scorable", "unknown", "insufficient"}:
        reward_status = "scorable" if run_ids else "insufficient"
    failure_class = receipt.get("reason")
    if verification["missing_count"] > 0:
        failure_class = failure_class or "verification_incomplete"
    elif reward_status != "scorable":
        failure_class = failure_class or "reward_not_scorable"
    scores: dict[str, dict[str, dict[str, Any]]] = {}
    missing_score = False
    for run_id in run_ids:
        scores[run_id] = {}
        for language in ("en", "ja"):
            path = (
                skill_dir
                / "state/runs"
                / run_id
                / "gates"
                / f"beat-rate-{language}.json"
            )
            try:
                score = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                missing_score = True
                scores[run_id][language] = {
                    "status": "insufficient",
                    "chosen_beat_rate": None,
                    "selection_inverted": None,
                    "partial": None,
                    "calls_used": None,
                }
                continue
            if (
                score.get("run_id") != run_id
                or score.get("lang") != language
            ):
                raise ValueError("beat-rate receipt identity mismatch")
            scores[run_id][language] = {
                "chosen_beat_rate": score.get("chosen_beat_rate"),
                "selection_inverted": score.get("selection_inverted"),
                "partial": score.get("partial"),
                "calls_used": score.get("calls_used"),
            }
    if missing_score:
        reward_status = "insufficient"
        if verification["missing_count"] == 0:
            failure_class = failure_class or "reward_not_scorable"
    return {
        "schema_version": 1,
        "event_id": f"article-self-improve:{observed_date}",
        "observed_at": observed_at,
        "learning_status": learning_status,
        "reward_status": reward_status,
        "decision": decision,
        "change": (
            receipt.get("changed_rules")
            or receipt.get("experiment_id")
        ),
        "weight_hash": weight_hash,
        "failure_class": failure_class,
        "verification": verification,
        "experiment_id": receipt.get("experiment_id"),
        "metric_run_ids": run_ids,
        "scores": scores,
        "source_commits": receipt.get("source_commits", []),
        "self_improve_receipt_sha256": hashlib.sha256(raw).hexdigest(),
    }


def build_message(event: dict[str, Any]) -> str:
    lines = [
        f"[{event['event_id']}] learning {event['learning_status']}",
        "runs=" + ",".join(event["metric_run_ids"]),
        f"reward={event['reward_status']} decision={event['decision']}",
        f"change={event['change']} weight={event['weight_hash']}",
        f"failure={event['failure_class']}",
        f"verify_missing={event['verification']['missing_count']}",
    ]
    for run_id in event["metric_run_ids"]:
        parts = []
        for language in ("ja", "en"):
            score = event["scores"][run_id][language]
            parts.append(
                f"{language}={score['chosen_beat_rate']}"
                f"/inverted={str(score['selection_inverted']).lower()}"
            )
        lines.append(f"{run_id} " + " ".join(parts))
    if event.get("experiment_id"):
        lines.append(f"experiment={event['experiment_id']}")
    return "\n".join(lines)


def deliver(
    outbox: Path,
    event: dict[str, Any],
    transport: Transport,
) -> dict[str, Any]:
    try:
        current = json.loads(outbox.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = None
    identity = (
        "event_id",
        "metric_run_ids",
        "scores",
        "reward_status",
        "decision",
        "change",
        "weight_hash",
        "failure_class",
        "verification",
        "self_improve_receipt_sha256",
    )
    if isinstance(current, dict):
        if any(current.get(key) != event.get(key) for key in identity):
            raise ValueError("self-improve notification outbox conflicts")
        if current.get("status") == "sent" and current.get("message_id"):
            return current
        prepared = current
    else:
        prepared = {
            **event,
            "status": "prepared",
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write(outbox, prepared)
    message_id = transport(build_message(prepared))
    if not isinstance(message_id, str) or not message_id.strip():
        raise RuntimeError("Telegram transport returned no message ID")
    sent = {
        **prepared,
        "status": "sent",
        "message_id": message_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write(outbox, sent)
    return sent


def writer_telegram_transport(target: str) -> Transport:
    return telegram_api_transport(target)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-dir", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument(
        "--target",
        default=(
            os.environ.get("ARTICLE_TELEGRAM_TARGET")
            or os.environ.get("TELEGRAM_TARGET_ID")
            or "8547730585"
        ),
    )
    parser.add_argument("--fixture-receipt", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        event = load_event(args.skill_dir, args.receipt)
        date = event["event_id"].rsplit(":", 1)[-1]
        outbox = (
            args.skill_dir
            / "state/learning/notifications"
            / f"{date}.json"
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
        print(f"self-improve notification pending: {error}", file=sys.stderr)
        return 75
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
