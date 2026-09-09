#!/usr/bin/env python3
"""Deterministic, compact CEO unit-economics snapshot builder.

Provider-reported tokens and provider-reported cost are deliberately separate.
An absent cost is unknown, never zero. Raw usage/revenue/contract rows are not
copied into the snapshot.
"""
from __future__ import annotations

import glob
import json
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
ACTIVE_PAID_STATUSES = {"paid", "purchased", "contracted", "in_progress"}


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return rows
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _expand_path(value: str, base: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    return expanded if expanded.is_absolute() else base / expanded


def _jst_date(value: Any) -> date | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), tz=JST).date()
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(JST).date()
    except (TypeError, ValueError, OverflowError):
        return None


def _configured_source_path(source: dict[str, Any], base: Path) -> Path | None:
    if isinstance(source.get("path"), str):
        return _expand_path(source["path"], base)
    pattern = source.get("path_glob")
    if not isinstance(pattern, str):
        return None
    expanded = str(_expand_path(pattern, base))
    matches = [Path(value) for value in glob.glob(expanded)]
    files = [path for path in matches if path.is_file()]
    return max(files, key=lambda path: path.stat().st_mtime) if files else None


def _paid_protection(source: dict[str, Any] | None, base: Path) -> tuple[bool, str]:
    if not source:
        return False, "not_configured"
    fail_closed = source.get("fail_closed", True) is not False
    try:
        path = _configured_source_path(source, base)
        if path is None or not path.is_file():
            return fail_closed, "source_missing"
        max_age = source.get("max_age_seconds")
        if isinstance(max_age, (int, float)) and time.time() - path.stat().st_mtime > float(max_age):
            return fail_closed, "source_stale"
        value = _read_json(path)
        items = value.get("items") if isinstance(value, dict) else None
        if not isinstance(items, list):
            return fail_closed, "source_malformed"
        for item in items:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").lower()
            delivery_action = str(item.get("delivery_action") or "").lower()
            if status in ACTIVE_PAID_STATUSES and delivery_action not in {"none", "completed", "closed"}:
                return True, "active_paid_obligation"
        return False, "no_active_paid_obligation"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return fail_closed, "source_malformed"


def _usage_by_loop_day(rows: list[dict[str, Any]], start: date, end: date) -> dict[str, dict[date, dict[str, Any]]]:
    result: dict[str, dict[date, dict[str, Any]]] = defaultdict(lambda: defaultdict(lambda: {
        "attempts": 0,
        "measured_attempts": 0,
        "unavailable_attempts": 0,
        "total_tokens": 0,
        "actual_cost_measured_attempts": 0,
        "actual_cost_usd": 0.0,
    }))
    for row in rows:
        loop = row.get("loop")
        day = _jst_date(row.get("timestamp"))
        if not isinstance(loop, str) or not loop or day is None or not start <= day <= end:
            continue
        cell = result[loop][day]
        cell["attempts"] += 1
        tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
        total = tokens.get("total")
        if row.get("measurement") == "provider_reported" and isinstance(total, int) and not isinstance(total, bool):
            cell["measured_attempts"] += 1
            cell["total_tokens"] += total
        else:
            cell["unavailable_attempts"] += 1
        cost = row.get("provider_cost_usd")
        if (
            isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0
            and row.get("cost_basis") == "actual_billed"
        ):
            cell["actual_cost_measured_attempts"] += 1
            cell["actual_cost_usd"] += float(cost)
    return result


def _money_number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _economics_by_loop(
    rows: list[dict[str, Any]], start: date, end: date
) -> tuple[dict[str, dict[str, Decimal]], dict[str, dict[str, Decimal]]]:
    revenues: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    costs: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in rows:
        if row.get("specversion") == "1.0" and isinstance(row.get("data"), dict):
            data = row["data"]
            loop = data.get("loop")
            day = _jst_date(row.get("time"))
            currency = data.get("currency")
            amount_minor = data.get("amount_minor")
            exponent = data.get("minor_unit_exponent")
            event_type = row.get("type")
            if (
                not isinstance(loop, str) or not loop or day is None or not start <= day <= end
                or not isinstance(currency, str) or not currency
                or not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0
                or not isinstance(exponent, int) or isinstance(exponent, bool) or not 0 <= exponent <= 4
                or not isinstance(data.get("evidence"), str) or not data["evidence"].strip()
            ):
                continue
            amount = Decimal(amount_minor) / (Decimal(10) ** exponent)
            if event_type == "ai.anicca.unit_economics.revenue.settled.v1" and data.get("recognition") == "settled":
                revenues[loop][currency] += amount
            elif (
                event_type == "ai.anicca.unit_economics.cost.actual_billed.v1"
                and data.get("recognition") == "actual_billed"
                and isinstance(data.get("invoice_id"), str) and data["invoice_id"].strip()
                and isinstance(data.get("allocation_basis"), str) and data["allocation_basis"].strip()
            ):
                costs[loop][currency] += amount
            continue

        loop = row.get("loop")
        day = _jst_date(row.get("timestamp", row.get("ts")))
        amount = row.get("amount")
        if (
            not isinstance(loop, str) or not loop or day is None or not start <= day <= end
            or row.get("status") != "settled" or row.get("currency") != "USD"
            or not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0
            or not isinstance(row.get("evidence"), str) or not row["evidence"].strip()
        ):
            continue
        revenues[loop]["USD"] += Decimal(str(amount))
    return revenues, costs


def build_snapshot(
    base: Path | str,
    config: dict[str, Any],
    end_date: date | None = None,
    *,
    config_root: Path | str | None = None,
) -> dict[str, Any]:
    base = Path(base)
    config_root = Path(config_root) if config_root is not None else base
    end = end_date or (datetime.now(JST).date() - timedelta(days=1))
    window_days = int(config.get("window_days", 7))
    start = end - timedelta(days=window_days - 1)
    try:
        from .ceo_allocation import effective_registry
    except ImportError:  # Executed with lib/ directly on sys.path by repository CLIs.
        from ceo_allocation import effective_registry

    registry = effective_registry(config_root, base)
    loop_names = sorted(registry.get("loops", {}).keys())

    usage_path = _expand_path(str(config.get(
        "usage_ledger", "~/.local/state/anicca/telemetry/agent-usage.jsonl"
    )), base)
    revenue_path = _expand_path(str(config.get("revenue_ledger", "ledgers/revenue-events.jsonl")), base)
    usage = _usage_by_loop_day(_read_jsonl(usage_path), start, end)
    economics_path_value = config.get("economics_ledger")
    economics_rows = _read_jsonl(revenue_path)
    if isinstance(economics_path_value, str):
        economics_path = _expand_path(economics_path_value, base)
        if economics_path != revenue_path:
            economics_rows.extend(_read_jsonl(economics_path))
    revenue, invoice_cost = _economics_by_loop(economics_rows, start, end)
    min_coverage = float(config.get("min_usage_coverage", 0.9))
    min_complete_days = int(config.get("min_complete_days", window_days))
    paid_sources = config.get("paid_obligation_sources")
    if not isinstance(paid_sources, dict):
        paid_sources = {}
    protected_loops = config.get("allocation_protected_loops")
    if not isinstance(protected_loops, list):
        protected_loops = []
    protected_loops = {value for value in protected_loops if isinstance(value, str)}

    loops: dict[str, Any] = {}
    dates = [start + timedelta(days=index) for index in range(window_days)]
    for loop in loop_names:
        daily = []
        for day in dates:
            cell = usage.get(loop, {}).get(day, {})
            daily.append({
                "date_jst": day.isoformat(),
                "attempts": int(cell.get("attempts", 0)),
                "measured_attempts": int(cell.get("measured_attempts", 0)),
                "unavailable_attempts": int(cell.get("unavailable_attempts", 0)),
                "total_tokens": int(cell.get("total_tokens", 0)),
                "actual_cost_measured_attempts": int(cell.get("actual_cost_measured_attempts", 0)),
                "actual_cost_usd": round(float(cell.get("actual_cost_usd", 0.0)), 8),
            })
        attempts = sum(row["attempts"] for row in daily)
        measured = sum(row["measured_attempts"] for row in daily)
        unavailable = sum(row["unavailable_attempts"] for row in daily)
        cost_measured = sum(row["actual_cost_measured_attempts"] for row in daily)
        complete_days = sum(1 for row in daily if row["attempts"] > 0)
        usage_coverage = measured / attempts if attempts else 0.0
        cost_coverage = cost_measured / attempts if attempts else 0.0
        actual_cost = round(sum(row["actual_cost_usd"] for row in daily), 8)
        revenue_by_currency = {
            currency: _money_number(amount)
            for currency, amount in sorted(revenue.get(loop, {}).items())
        }
        invoice_cost_by_currency = {
            currency: _money_number(amount)
            for currency, amount in sorted(invoice_cost.get(loop, {}).items())
        }
        settled_revenue_value = revenue.get(loop, {}).get("USD")
        settled_revenue = round(float(settled_revenue_value), 8) if settled_revenue_value is not None else 0.0
        revenue_available = settled_revenue_value is not None
        if loop in protected_loops:
            paid_protected, paid_state = True, "allocation_policy_protected"
        else:
            paid_protected, paid_state = _paid_protection(paid_sources.get(loop), base)

        reasons = []
        if complete_days < min_complete_days:
            reasons.append("insufficient_complete_days")
        if usage_coverage < min_coverage:
            reasons.append("usage_coverage_below_threshold")
        if cost_coverage < 1.0:
            reasons.append("actual_cost_unavailable")
        if not revenue_available:
            reasons.append(
                "settled_revenue_usd_unavailable" if revenue_by_currency else "settled_revenue_unavailable"
            )
        if paid_protected:
            reasons.append("active_paid_obligation" if paid_state == "active_paid_obligation" else "paid_obligation_state_unavailable")
        if config.get("mode") != "active":
            reasons.append("ceo_observe_only")

        if revenue_available and cost_coverage == 1.0 and attempts > 0:
            profit = round(settled_revenue - actual_cost, 8)
            profitability = "positive" if profit > 0 else "negative" if profit < 0 else "break_even"
        else:
            profit = None
            profitability = "unknown"

        loops[loop] = {
            "days": daily,
            "complete_days": complete_days,
            "attempts": attempts,
            "measured_attempts": measured,
            "unavailable_attempts": unavailable,
            "usage_coverage": round(usage_coverage, 8),
            "total_tokens": sum(row["total_tokens"] for row in daily),
            "actual_cost_measured_attempts": cost_measured,
            "actual_cost_coverage": round(cost_coverage, 8),
            "actual_cost_usd": actual_cost if cost_measured else None,
            "settled_revenue_usd": settled_revenue if revenue_available else None,
            "settled_revenue_by_currency": revenue_by_currency,
            "actual_invoice_cost_by_currency": invoice_cost_by_currency,
            "profitability_status": profitability,
            "profit_usd": profit,
            "paid_obligation_protected": paid_protected,
            "paid_obligation_state": paid_state,
            "allocation_eligible": not reasons,
            "eligibility_reasons": reasons,
        }

    return {
        "version": 1,
        "mode": config.get("mode", "observe_only"),
        "window": {"start_jst": start.isoformat(), "end_jst": end.isoformat(), "days": window_days},
        "thresholds": {"min_complete_days": min_complete_days, "min_usage_coverage": min_coverage},
        "loops": loops,
    }


def validate_allocation_policy(snapshot: dict[str, Any], loop: str, status: str) -> list[str]:
    errors: list[str] = []
    if snapshot.get("mode") != "active":
        errors.append("ceo_observe_only")
    loop_snapshot = snapshot.get("loops", {}).get(loop)
    if not isinstance(loop_snapshot, dict):
        return errors + ["unit_economics_snapshot_missing_loop"]
    if not loop_snapshot.get("allocation_eligible"):
        errors.append("unit_economics_ineligible")
    if status in {"paused", "reduce"} and loop_snapshot.get("paid_obligation_protected"):
        errors.append("paid_obligation_protected")
    return errors
