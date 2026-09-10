"""Pure, fail-closed net-performance projection for sealed investment receipts."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

from risk_policy import parse_instant


REQUIRED = frozenset({
    "schema_version", "period_start", "observed_at", "starting_nav_usd",
    "ending_nav_usd", "owner_cash_flow_usd", "fees_usd", "slippage_usd",
    "peak_adjusted_nav_usd", "gross_exposure_usd", "benchmark_start_price_usd",
    "benchmark_end_price_usd", "realized_pnl_usd", "unrealized_pnl_usd",
    "completed_round_trips", "source_receipt_ids",
})
MONEY = Decimal("0.01")
RATE = Decimal("0.000001")
MIN_STATISTICAL_ROUND_TRIPS = 30


def _number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError from error
    if not number.is_finite():
        raise ValueError
    return number


def _blocked(reason: str) -> dict[str, Any]:
    return {
        "capital_expansion_allowed": False,
        "measurement_status": "blocked",
        "reason": reason,
        "schema_version": 1,
    }


def project(snapshot: Any) -> dict[str, Any]:
    """Return deterministic metrics or one non-numeric fail-closed receipt."""
    if not isinstance(snapshot, dict) or set(snapshot) != REQUIRED:
        return _blocked("required_metric_missing")
    if snapshot.get("schema_version") != 1:
        return _blocked("performance_schema_invalid")
    try:
        period_start = parse_instant(snapshot["period_start"])
        observed_at = parse_instant(snapshot["observed_at"])
    except (KeyError, TypeError, ValueError):
        return _blocked("performance_time_invalid")
    if period_start > observed_at:
        return _blocked("performance_time_invalid")
    receipts = snapshot.get("source_receipt_ids")
    if (not isinstance(receipts, list) or not receipts
            or any(not isinstance(item, str) or not item for item in receipts)
            or len(set(receipts)) != len(receipts)):
        return _blocked("performance_source_invalid")
    try:
        start = _number(snapshot["starting_nav_usd"])
        end = _number(snapshot["ending_nav_usd"])
        cash_flow = _number(snapshot["owner_cash_flow_usd"])
        fees = _number(snapshot["fees_usd"])
        slippage = _number(snapshot["slippage_usd"])
        peak = _number(snapshot["peak_adjusted_nav_usd"])
        exposure = _number(snapshot["gross_exposure_usd"])
        benchmark_start = _number(snapshot["benchmark_start_price_usd"])
        benchmark_end = _number(snapshot["benchmark_end_price_usd"])
        realized = _number(snapshot["realized_pnl_usd"])
        unrealized = _number(snapshot["unrealized_pnl_usd"])
        round_trips = snapshot["completed_round_trips"]
    except ValueError:
        return _blocked("performance_number_invalid")
    if isinstance(round_trips, bool) or not isinstance(round_trips, int):
        return _blocked("performance_sample_invalid")
    adjusted_end = end - cash_flow
    nav_net = adjusted_end - start
    capital_base = start if start > 0 else max(cash_flow, Decimal("0"))
    component_net = realized + unrealized
    if (start < 0 or capital_base <= 0 or benchmark_start <= 0 or benchmark_end <= 0
            or fees < 0 or slippage < 0 or exposure < 0
            or round_trips < 0 or peak < max(start, adjusted_end)
            or abs(component_net - nav_net) > MONEY):
        return _blocked("performance_invariant_invalid")
    net = component_net
    gross = net + fees + slippage
    drawdown = peak - adjusted_end
    net_return = net / capital_base
    benchmark_return = (benchmark_end - benchmark_start) / benchmark_start
    benchmark_pnl = capital_base * benchmark_return
    alpha = net - benchmark_pnl
    statistically_supported = round_trips >= MIN_STATISTICAL_ROUND_TRIPS

    def money(value: Decimal) -> str:
        return str(value.quantize(MONEY, rounding=ROUND_HALF_EVEN))

    def rate(value: Decimal) -> str:
        return str(value.quantize(RATE, rounding=ROUND_HALF_EVEN))

    return {
        "alpha_pnl_usd": money(alpha),
        "benchmark_pnl_usd": money(benchmark_pnl),
        "benchmark_return": rate(benchmark_return),
        "capital_cap_usd": "100.00",
        "capital_expansion_allowed": False,
        "completed_round_trips": round_trips,
        "fees_usd": money(fees),
        "gross_exposure_usd": money(exposure),
        "gross_strategy_pnl_usd": money(gross),
        "max_drawdown_usd": money(drawdown),
        "measurement_status": "measured",
        "net_pnl_usd": money(net),
        "net_return": rate(net_return),
        "observed_at": snapshot["observed_at"],
        "owner_cash_flow_usd": money(cash_flow),
        "period_start": snapshot["period_start"],
        "realized_pnl_usd": money(realized),
        "schema_version": 1,
        "slippage_usd": money(slippage),
        "statistically_supported": statistically_supported,
        "source_receipt_ids": receipts,
        "unrealized_pnl_usd": money(unrealized),
        "reason": ("net_negative_and_statistically_unsupported" if net < 0 and not statistically_supported
                   else "net_negative" if net < 0 else "statistically_unsupported"
                   if not statistically_supported else "capital_expansion_not_scheduled"),
    }
