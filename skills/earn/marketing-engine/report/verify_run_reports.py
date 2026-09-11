#!/usr/bin/env python3
"""Verify Gate 6 final-run and Telegram-delivery evidence for every current lane."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sys

import run_contract


def _duplicates(rows: list[dict]) -> list[str]:
    counts = collections.Counter(f"{r.get('runner_id')}:{r.get('run_id')}" for r in rows)
    return sorted(key for key, count in counts.items() if count != 1)


def verify(state_root: pathlib.Path, expected: dict[str, str]) -> dict:
    state_root = pathlib.Path(state_root)
    store = run_contract.RunStore(
        state_root / "run-reports.jsonl", state_root / "run-deliveries.jsonl")
    events = store.final_events()
    deliveries = store.deliveries()
    duplicate_final = _duplicates(events)
    duplicate_delivery = _duplicates(deliveries)
    event_by_key = {(r.get("runner_id"), r.get("run_id")): r for r in events}
    delivery_by_key = {(r.get("runner_id"), r.get("run_id")): r for r in deliveries}
    errors = []
    details = {}
    would_resend = []

    if set(expected) != set(run_contract.RUNNERS):
        errors.append("expected map does not contain exactly eight runner IDs")
    if duplicate_final:
        errors.append("duplicate final records")
    if duplicate_delivery:
        errors.append("duplicate delivery records")

    for runner_id in sorted(run_contract.RUNNERS):
        run_id = expected.get(runner_id)
        key = (runner_id, run_id)
        event = event_by_key.get(key)
        delivery = delivery_by_key.get(key)
        runner_errors = []
        if event is None:
            runner_errors.append("final_event_missing")
        else:
            try:
                run_contract.validate_event(event)
            except run_contract.ContractError as exc:
                runner_errors.append(f"contract:{exc}")
            for item in event.get("evidence", []):
                path = pathlib.Path(item["path"])
                if not path.is_file():
                    runner_errors.append(f"evidence_missing:{path}")
                    continue
                payload = path.read_bytes()
                if len(payload) != item["bytes"]:
                    runner_errors.append(f"evidence_size_mismatch:{path}")
                if hashlib.sha256(payload).hexdigest() != item["sha256"]:
                    runner_errors.append(f"evidence_hash_mismatch:{path}")
        if delivery is None:
            runner_errors.append("telegram_delivery_missing")
        elif (delivery.get("status") != "delivered" or
              not delivery.get("message_ids") or
              delivery.get("chat_id") is None):
            runner_errors.append("telegram_delivery_invalid")

        if event is not None and delivery is not None:
            calls = []

            def forbidden_send(_text):
                calls.append(True)
                return {"status": "delivered", "chat_id": -1, "message_ids": [-1]}

            replay = run_contract.record_and_deliver(event, store, forbidden_send)
            if calls or replay.get("message_ids") != delivery.get("message_ids"):
                would_resend.append(runner_id)
                runner_errors.append("dedup_replay_would_send")

        details[runner_id] = {
            "run_id": run_id,
            "status": event.get("status") if event else None,
            "dry_run": event.get("dry_run") if event else None,
            "message_ids": delivery.get("message_ids") if delivery else [],
            "evidence_items": len(event.get("evidence", [])) if event else 0,
            "errors": runner_errors,
        }
        errors.extend(f"{runner_id}:{message}" for message in runner_errors)

    return {
        "schema_version": "marketing.gate6-verification.v1",
        "passed": not errors,
        "runners_verified": sum(1 for item in details.values() if not item["errors"]),
        "expected_run_ids": expected,
        "duplicate_final_keys": duplicate_final,
        "duplicate_delivery_keys": duplicate_delivery,
        "would_resend": would_resend,
        "details": details,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=pathlib.Path, required=True)
    parser.add_argument("--expected", type=pathlib.Path, required=True,
                        help="JSON object mapping all eight runner IDs to run IDs")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args(argv)
    result = verify(args.state_root, json.loads(args.expected.read_text()))
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
