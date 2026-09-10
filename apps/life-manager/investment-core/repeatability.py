#!/usr/bin/env python3
"""Measure the L11 natural-wake evidence without creating broker effects."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alpaca_cli import _context, _run
from effect_store import unresolved_intent_count
from risk_policy import parse_instant


NY = ZoneInfo("America/New_York")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("repeatability_event_ledger_invalid") from error
    if not all(isinstance(value, dict) for value in values):
        raise ValueError("repeatability_event_ledger_invalid")
    return values


def _outbox(path: Path, start: datetime, end: datetime | None = None) -> list[dict[str, Any]]:
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT event_key,status,attempt_count,provider_message_id,created_at,delivered_at,"
            "last_error_code FROM telegram_outbox").fetchall()
    except sqlite3.Error as error:
        raise ValueError("repeatability_outbox_invalid") from error
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    keys = ("event_key", "status", "attempt_count", "provider_message_id", "created_at",
            "delivered_at", "last_error_code")
    return [dict(zip(keys, row)) for row in rows
            if parse_instant(row[4]) >= start
            and (end is None or parse_instant(row[4]) <= end)]


def _consecutive_days(days: set[date]) -> bool:
    if not days:
        return False
    expected = {min(days) + timedelta(days=index)
                for index in range((max(days) - min(days)).days + 1)}
    return days == expected


def _official_orders(credentials: Path, cli: Path) -> dict[str, Any]:
    env = _context(credentials, cli, "live")
    orders = _run(cli, ["order", "list", "--quiet", "--status", "all", "--limit", "500",
        "--jq", "[.[]|select(.client_order_id|startswith(\"lm-ai-\"))|"
        "{id,client_order_id,status,symbol,side}]"], env)
    if not isinstance(orders, list):
        raise ValueError("repeatability_official_orders_invalid")
    clients = [row.get("client_order_id") for row in orders]
    ids = [row.get("id") for row in orders]
    if any(not isinstance(value, str) for value in [*clients, *ids]):
        raise ValueError("repeatability_official_orders_invalid")
    return {"count": len(orders), "duplicate_client_ids": len(clients) - len(set(clients)),
            "duplicate_order_ids": len(ids) - len(set(ids))}


def evaluate(*, shadow_state: Path, live_state: Path, start: datetime,
             required_days: int, required_wakes: int,
             credentials: Path, cli: Path, shadow_end: datetime | None = None,
             live_start: datetime | None = None) -> dict[str, Any]:
    if (shadow_end is None) != (live_start is None) \
            or (shadow_end is not None and (shadow_end < start or live_start <= shadow_end)):
        raise ValueError("repeatability_transition_invalid")
    live_begin = live_start or start
    shadow_events = [row for row in _jsonl(shadow_state / "events.jsonl")
                     if row.get("run_id") != "install"
                     and row.get("provider") == "shared-agent-runner"
                     and parse_instant(row["timestamp"]) >= start
                     and (shadow_end is None or parse_instant(row["timestamp"]) <= shadow_end)]
    live_events = [row for row in _jsonl(live_state / "events.jsonl")
                   if row.get("run_id") != "install"
                   and row.get("provider") == "shared-agent-runner"
                   and parse_instant(row["timestamp"]) >= live_begin]
    events = [*shadow_events, *live_events]
    terminal = [row for row in events if row.get("phase") == "report"]
    source_windows = [
        (shadow_events, _outbox(shadow_state / "telegram-outbox.sqlite3", start, shadow_end)),
        (live_events, _outbox(live_state / "telegram-outbox.sqlite3", live_begin)),
    ]
    days = {parse_instant(row["timestamp"]).astimezone(NY).date() for row in terminal}
    event_ids = [row.get("event_id") for row in events]
    terminal_runs = [row.get("run_id") for row in terminal]
    pids = {match.group(1) for row in terminal
            if (match := re.search(r"-(\d+)$", str(row.get("run_id"))))}
    delivered = []
    deliveries_by_run = []
    for source_events, source_outbox in source_windows:
        source_delivered = [row for row in source_outbox
                            if str(row["event_key"]).startswith(
                                ("alpaca-wake:", "alpaca-failure:"))
                            and row["status"] == "delivered"
                            and row["provider_message_id"] and row["delivered_at"]
                            and row["last_error_code"] is None]
        delivered.extend(source_delivered)
        execute_by_run = {row.get("run_id"): parse_instant(row["timestamp"])
                          for row in source_events if row.get("phase") == "execute"}
        for row in source_events:
            if row.get("phase") != "report":
                continue
            execute_at = execute_by_run.get(row.get("run_id"))
            report_at = parse_instant(row["timestamp"])
            deliveries_by_run.append(0 if execute_at is None else sum(
                execute_at <= parse_instant(delivery["created_at"]) <= report_at
                for delivery in source_delivered))
    official = _official_orders(credentials, cli)
    calendar_days = len(days) >= required_days and _consecutive_days(days)
    natural_wakes = len(terminal) >= required_wakes
    weekend_observed = any(day.weekday() >= 5 for day in days)
    checks = {
        "calendar_days": calendar_days,
        "natural_wakes": natural_wakes,
        "repeatability_window": natural_wakes or (calendar_days and weekend_observed),
        "telegram_every_wake": len(delivered) == len(terminal)
                               and all(count == 1 for count in deliveries_by_run),
        "unique_runtime_events": len(event_ids) == len(set(event_ids)),
        "one_terminal_per_run": len(terminal_runs) == len(set(terminal_runs)),
        "multiple_processes": len(pids) >= 2,
        "weekend_observed": weekend_observed,
        "shadow_no_effect": all(row.get("effect_class") == "none"
                                and row.get("effect_status") == "not_applicable"
                                for row in shadow_events),
        "live_unresolved_zero": unresolved_intent_count(live_state / "receipts.jsonl") == 0,
        "official_duplicates_zero": official["duplicate_client_ids"] == 0
                                    and official["duplicate_order_ids"] == 0,
    }
    required_checks = {name: value for name, value in checks.items()
                       if name not in {"calendar_days", "natural_wakes", "weekend_observed"}}
    return {"status": "pass" if all(required_checks.values()) else "collecting", "checks": checks,
            "window_start": start.isoformat(),
            "transition": {"shadow_end": shadow_end.isoformat() if shadow_end else None,
                           "live_start": live_start.isoformat() if live_start else None},
            "observed": {"calendar_days": len(days), "natural_wakes": len(terminal),
                         "shadow_wakes": sum(row.get("phase") == "report"
                                             for row in shadow_events),
                         "live_wakes": sum(row.get("phase") == "report"
                                           for row in live_events),
                         "failed_wakes": sum(row.get("status") == "fail" for row in terminal),
                         "delivered_reports": len(delivered), "processes": len(pids),
                         "unreported_wakes": sum(count != 1 for count in deliveries_by_run),
                         "first_ny_day": min(days).isoformat() if days else None,
                         "last_ny_day": max(days).isoformat() if days else None,
                         "official_orders": official["count"]},
            "required": {"calendar_days": required_days, "natural_wakes": required_wakes},
            "evaluated_at": datetime.now().astimezone().isoformat()}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, path)
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-state", type=Path, required=True)
    parser.add_argument("--live-state", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--required-days", type=int, default=30)
    parser.add_argument("--required-wakes", type=int, default=100)
    parser.add_argument("--shadow-end")
    parser.add_argument("--live-start")
    args = parser.parse_args()
    if args.required_days < 1 or args.required_wakes < 1:
        raise ValueError("repeatability_requirement_invalid")
    result = evaluate(shadow_state=args.shadow_state, live_state=args.live_state,
                      start=parse_instant(args.start), required_days=args.required_days,
                      required_wakes=args.required_wakes, credentials=args.credentials, cli=args.cli,
                      shadow_end=parse_instant(args.shadow_end) if args.shadow_end else None,
                      live_start=parse_instant(args.live_start) if args.live_start else None)
    _write(args.live_state / "repeatability-latest.json", result)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "pass" else 75


if __name__ == "__main__":
    raise SystemExit(main())
