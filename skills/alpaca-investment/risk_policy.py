"""Deterministic owner-capital entry boundary; strategy judgment stays with the model."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any
from zoneinfo import ZoneInfo


CAPITAL_CAP = Decimal("100.00")
TRADE_LOSS_CAP = Decimal("10.00")
DAILY_LOSS_CAP = Decimal("20.00")
MAX_AGE_SECONDS = Decimal("30")


def _number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError from error
    if not number.is_finite():
        raise ValueError
    return number


def parse_instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError
    normalized = re.sub(
        r"(\.\d{6})\d+(?=(?:Z|[+-]\d{2}:\d{2})$)", r"\1", value
    )
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(timezone.utc)


def evaluate_entry(snapshot: dict[str, Any], max_loss_usd: Any,
                   *, now: datetime | None = None) -> dict[str, Any]:
    limits = {"allocated_capital_usd": "100.00", "daily_loss_usd": "20.00",
              "trade_max_loss_usd": "10.00"}
    checks = {"allocated_capital": False, "daily_loss": False, "fresh": False,
              "new_york_day": False, "trade_max_loss": False}
    try:
        if not isinstance(snapshot, dict):
            raise ValueError
        loss = _number(max_loss_usd)
        allocated = _number(snapshot.get("allocated_capital_usd"))
        _number(snapshot.get("cash_flow_ny_day_usd"))
        equity_pnl = _number(snapshot.get("equity_pnl_ny_day_usd"))
        official_pnl = _number(snapshot.get("official_pnl_ny_day_usd"))
        if snapshot.get("risk_day_ready") is not True:
            raise ValueError
        observed = parse_instant(snapshot.get("observed_at"))
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        ny_day = current.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        checks = {
            "allocated_capital": allocated >= 0 and allocated + loss <= CAPITAL_CAP,
            "daily_loss": min(equity_pnl, official_pnl) > -DAILY_LOSS_CAP,
            "fresh": Decimal(str((current - observed).total_seconds())) >= 0
                     and Decimal(str((current - observed).total_seconds())) <= MAX_AGE_SECONDS,
            "new_york_day": snapshot.get("ny_day") == ny_day,
            "trade_max_loss": loss > 0 and loss <= TRADE_LOSS_CAP,
        }
    except (ValueError, OverflowError):
        pass
    approved = all(checks.values())
    return {"approved": approved, "checks": checks,
            "gate": "fixed_risk_approved" if approved else "fixed_risk_rejected",
            "limits": limits}
