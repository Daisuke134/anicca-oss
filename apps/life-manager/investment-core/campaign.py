"""Official reconciliation of the existing SPY paper option campaign."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


RUN_REF = "a08-canary-2"
CANDIDATE_REF = "alpaca-option-spread://SPY/2026-09-08/769C-770C"
BUY_SYMBOL = "SPY260908C00769000"
SELL_SYMBOL = "SPY260908C00770000"
SYMBOLS = (BUY_SYMBOL, SELL_SYMBOL)


def _number(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("campaign_number_invalid") from error
    if not number.is_finite():
        raise ValueError("campaign_number_invalid")
    return number


def reconcile(snapshot: dict[str, Any]) -> dict[str, Any]:
    if snapshot.get("paper") is not True or snapshot.get("unexpected_positions"):
        raise ValueError("campaign_position_scope_invalid")
    fills, positions = snapshot.get("fills"), snapshot.get("positions")
    if not isinstance(fills, list) or not isinstance(positions, list):
        raise ValueError("campaign_shape_invalid")
    net = {symbol: Decimal("0") for symbol in SYMBOLS}
    cash_flow = Decimal("0")
    for fill in fills:
        if not isinstance(fill, dict) or fill.get("symbol") not in net:
            raise ValueError("campaign_fill_invalid")
        quantity, price = _number(fill.get("qty")), _number(fill.get("price"))
        if quantity <= 0 or price <= 0 or fill.get("side") not in {"buy", "sell"}:
            raise ValueError("campaign_fill_invalid")
        sign = Decimal("1") if fill["side"] == "buy" else Decimal("-1")
        net[fill["symbol"]] += sign * quantity
        cash_flow -= sign * quantity * price * 100
    official = {position.get("symbol"): _number(position.get("qty")) for position in positions
                if isinstance(position, dict) and position.get("symbol") in net}
    if any(net[symbol] != official.get(symbol, Decimal("0")) for symbol in SYMBOLS):
        raise ValueError("campaign_fill_position_mismatch")
    unrealized = sum((_number(position.get("unrealized_pl")) for position in positions), Decimal("0"))
    status = "CLOSED" if not positions and fills else "OPEN"
    options = {row.get("symbol"): row for row in snapshot.get("options", []) if isinstance(row, dict)}
    exit_credit = None
    if status == "OPEN" and all(symbol in options for symbol in SYMBOLS):
        exit_credit = _number(options[BUY_SYMBOL].get("bid")) - _number(options[SELL_SYMBOL].get("ask"))
    regular_open = snapshot.get("clock", {}).get("is_open") is True
    exit_status = "CLOSED" if status == "CLOSED" else (
        "HOLD_CLOSED_SESSION" if not regular_open else
        "EXIT_READY" if exit_credit is not None and exit_credit > 0 else "HOLD_INVALID_CREDIT"
    )
    account = snapshot.get("account", {})
    result = {
        "account": account,
        "candidate_ref": CANDIDATE_REF,
        "entry_cash_flow_usd": str(cash_flow.quantize(Decimal("0.01"))),
        "exit_credit_usd": str(exit_credit.quantize(Decimal("0.01"))) if exit_credit is not None else None,
        "exit_status": exit_status,
        "fills": fills,
        "observed_at": snapshot.get("clock", {}).get("observed_at"),
        "paper": True,
        "positions": positions,
        "run_ref": RUN_REF,
        "status": status,
        "structure": "bull_call_debit_spread",
        "symbols": list(SYMBOLS),
        "unrealized_pnl_usd": str(unrealized.quantize(Decimal("0.01"))),
    }
    if status == "CLOSED":
        result["realized_pnl_usd"] = result["entry_cash_flow_usd"]
    return result


def exit_order(campaign: dict[str, Any]) -> dict[str, Any]:
    if campaign.get("exit_status") != "EXIT_READY":
        raise ValueError("campaign_exit_not_ready")
    credit = _number(campaign.get("exit_credit_usd"))
    if credit <= 0:
        raise ValueError("campaign_exit_credit_invalid")
    return {
        "asset_class": "option_spread_close",
        "limit_price": str((-credit).quantize(Decimal("0.01"))),
        "long_symbol": BUY_SYMBOL,
        "short_symbol": SELL_SYMBOL,
        "time_in_force": "day",
        "type": "limit",
    }
