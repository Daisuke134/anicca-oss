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


def _outbox(path: Path, start: datetime) -> list[dict[str, Any]]:
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
    return [dict(zip(keys, row)) for row in rows if parse_instant(row[4]) >= start]


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
             credentials: Path, cli: Path) -> dict[str, Any]:
    events = [row for row in _jsonl(shadow_state / "events.jsonl")
              if row.get("run_id") != "install" and parse_instant(row["timestamp"]) >= start]
    terminal = [row for row in events if row.get("phase") == "report"]
    outbox = _outbox(shadow_state / "telegram-outbox.sqlite3", start)
    days = {parse_instant(row["timestamp"]).astimezone(NY).date() for row in terminal}
    event_ids = [row.get("event_id") for row in events]
    terminal_runs = [row.get("run_id") for row in terminal]
    pids = {match.group(1) for row in terminal
            if (match := re.search(r"-(\d+)$", str(row.get("run_id"))))}
    delivered = [row for row in outbox if str(row["event_key"]).startswith("alpaca-wake:")
                 and row["status"] == "delivered"
                 and row["provider_message_id"] and row["delivered_at"]
                 and row["last_error_code"] is None]
    execute_by_run = {row.get("run_id"): parse_instant(row["timestamp"])
                      for row in events if row.get("phase") == "execute"}
    deliveries_by_run = []
    for row in terminal:
        execute_at = execute_by_run.get(row.get("run_id"))
        report_at = parse_instant(row["timestamp"])
        deliveries_by_run.append(0 if execute_at is None else sum(
            execute_at <= parse_instant(delivery["created_at"]) <= report_at
            for delivery in delivered))
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
                                and row.get("effect_status") == "not_applicable" for row in events),
        "live_unresolved_zero": unresolved_intent_count(live_state / "receipts.jsonl") == 0,
        "official_duplicates_zero": official["duplicate_client_ids"] == 0
                                    and official["duplicate_order_ids"] == 0,
    }
    required_checks = {name: value for name, value in checks.items()
                       if name not in {"calendar_days", "natural_wakes", "weekend_observed"}}
    return {"status": "pass" if all(required_checks.values()) else "collecting", "checks": checks,
            "window_start": start.isoformat(),
            "observed": {"calendar_days": len(days), "natural_wakes": len(terminal),
                         "delivered_reports": len(delivered), "processes": len(pids),
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
    args = parser.parse_args()
    if args.required_days < 1 or args.required_wakes < 1:
        raise ValueError("repeatability_requirement_invalid")
    result = evaluate(shadow_state=args.shadow_state, live_state=args.live_state,
                      start=parse_instant(args.start), required_days=args.required_days,
                      required_wakes=args.required_wakes, credentials=args.credentials, cli=args.cli)
    _write(args.live_state / "repeatability-latest.json", result)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "pass" else 75


if __name__ == "__main__":
    raise SystemExit(main())
