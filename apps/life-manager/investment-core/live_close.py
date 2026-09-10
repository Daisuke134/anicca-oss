#!/usr/bin/env python3
"""Execute or reconcile exactly one frozen close of the L09 BTC canary."""

from __future__ import annotations

import json
import fcntl
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from alpaca_cli import read_live_close, read_live_close_snapshot, submit_live_close
from control import control_fence, read_control
from effect_store import (effect_state, mark_started, record_terminal_outcome, seal,
                          unresolved_intent_count)
from live_canary import _write_result
from risk_policy import parse_instant


CLOSE_REF = "L10_LOCAL_CLOSE_V1"


def _paths() -> tuple[Path, Path, Path]:
    if os.environ.get("LIFE_MANAGER_INVESTMENT_MODE") != "live" \
            or os.environ.get("LIFE_MANAGER_INVESTMENT_DEPLOYMENT") != "local":
        raise ValueError("live_close_context_invalid")
    credentials = os.environ.get("ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE")
    state = os.environ.get("ALPACA_INVESTMENT_LIVE_STATE_DIR")
    cli = os.environ.get("ALPACA_CLI", "~/.local/bin/alpaca")
    if not credentials or not state:
        raise ValueError("live_close_context_invalid")
    return Path(credentials).expanduser(), Path(state).expanduser(), Path(cli).expanduser()


def _read_canary(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        order, position = value["order"], value["position"]
        qty = Decimal(str(position["qty"]))
        if (value.get("verified") is not True or value.get("status") != "verified"
                or order.get("client_order_id") != "lm-ai-3367d3a6cc659b8017a57f3d"
                or order.get("symbol") not in {"BTC/USDC", "BTCUSDC"}
                or position.get("symbol") != "BTCUSD" or qty <= 0):
            raise ValueError
    except (FileNotFoundError, json.JSONDecodeError, InvalidOperation, KeyError,
            TypeError, ValueError) as error:
        raise ValueError("live_close_parent_invalid") from error
    return value


def _position_qty(rows: list[dict], symbol: str) -> Decimal:
    values = [row for row in rows if row.get("symbol") == symbol]
    if len(values) != 1:
        raise ValueError("live_close_position_invalid")
    try:
        return Decimal(str(values[0]["qty"]))
    except (InvalidOperation, KeyError, TypeError) as error:
        raise ValueError("live_close_position_invalid") from error


def _new_plan(state: Path, snapshot: dict) -> dict:
    parent = _read_canary(state / "live-canary.json")
    buy_gross = sum((Decimal(str(fill["qty"])) * Decimal(str(fill["price"]))
                     for fill in parent["fills"]), Decimal("0"))
    buy_fees = [row for row in snapshot["fees"]
                if row.get("order_id") == parent["order"]["id"]]
    expected_buy_fee = Decimal(str(parent["position"]["qty"])) \
        - Decimal(str(parent["order"]["filled_qty"]))
    if (len(buy_fees) != 1 or buy_fees[0].get("symbol") != "BTCUSD"
            or Decimal(str(buy_fees[0].get("qty"))) != expected_buy_fee):
        raise ValueError("live_close_parent_fee_invalid")
    value = {
        "buy_fee_btc": str(-expected_buy_fee),
        "buy_gross_cost_usdc": str(buy_gross),
        "close_ref": CLOSE_REF,
        "frozen_qty": str(Decimal(str(parent["position"]["qty"]))),
        "parent_client_order_id": parent["order"]["client_order_id"],
        "parent_order_id": parent["order"]["id"],
        "pre_usdc_qty": str(_position_qty(snapshot["positions"], "USDCUSD")),
        "external_flow_fingerprint": snapshot["external_flow_fingerprint"],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    path = state / "live-close-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(state / ".live-close-plan.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            return _read_plan(path)
        _write_result(path, value)
        return value
    finally:
        os.close(lock)


def _read_plan(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (value.get("close_ref") != CLOSE_REF or Decimal(value["frozen_qty"]) <= 0
                or Decimal(value["pre_usdc_qty"]) <= 0
                or Decimal(value["buy_gross_cost_usdc"]) <= 0
                or Decimal(value["buy_fee_btc"]) < 0
                or not isinstance(value.get("external_flow_fingerprint"), str)
                or not isinstance(value.get("parent_client_order_id"), str)
                or not isinstance(value.get("parent_order_id"), str)):
            raise ValueError
        return value
    except (FileNotFoundError, json.JSONDecodeError, InvalidOperation, KeyError,
            TypeError, ValueError) as error:
        raise ValueError("live_close_plan_invalid") from error


def _gate(snapshot: dict, plan: dict, state: Path) -> None:
    account, asset, quote = snapshot["account"], snapshot["asset"], snapshot["quote"]
    try:
        btc = _position_qty(snapshot["positions"], "BTCUSD")
        usdc = _position_qty(snapshot["positions"], "USDCUSD")
        bid, ask = Decimal(str(quote["bp"])), Decimal(str(quote["ap"]))
        age = (datetime.now(timezone.utc) - parse_instant(quote["t"])).total_seconds()
        spread = (ask - bid) / ask
        valid_numbers = all(value.is_finite() and value > 0 for value in (btc, usdc, bid, ask))
    except (InvalidOperation, KeyError, TypeError, ValueError, ZeroDivisionError) as error:
        raise ValueError("live_close_gate_rejected") from error
    symbols = {row.get("symbol") for row in snapshot["positions"]}
    if (not valid_numbers or btc != Decimal(plan["frozen_qty"])
            or usdc != Decimal(plan["pre_usdc_qty"]) or symbols != {"BTCUSD", "USDCUSD"}
            or snapshot["open_orders"] or unresolved_intent_count(state / "receipts.jsonl") != 0
            or snapshot["external_flow_fingerprint"] != plan["external_flow_fingerprint"]
            or account.get("status") != "ACTIVE" or account.get("crypto_status") != "ACTIVE"
            or any(account.get(key) is True for key in
                   ("trading_blocked", "transfers_blocked", "account_blocked"))
            or asset.get("status") != "active" or asset.get("tradable") is not True
            or age < 0 or age > 30 or spread < 0 or spread > Decimal("0.15")):
        raise ValueError("live_close_gate_rejected")
    control = read_control(state / "control.json")
    if control["paused"] or control["killed"]:
        raise ValueError("live_close_control_rejected")


def _result(sealed: dict[str, str], broker: dict, submitted: bool) -> int:
    value = {"close_ref": CLOSE_REF, "client_order_id": sealed["client_order_id"],
             "effect_id": sealed["effect_id"], "status": broker["status"],
             "submitted_this_run": submitted, "verified": broker.get("verified") is True,
             "observed_at": datetime.now(timezone.utc).isoformat()}
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0 if value["verified"] else 75


def main() -> int:
    credentials, state, cli = _paths()
    plan_path = state / "live-close-plan.json"
    if plan_path.exists():
        plan = _read_plan(plan_path)
    else:
        plan = _new_plan(state, read_live_close_snapshot(
            credentials_path=credentials, cli_path=cli))
    order = {"asset_class": "crypto", "qty": plan["frozen_qty"], "side": "sell",
             "symbol": "BTC/USDC", "time_in_force": "gtc", "type": "market"}
    decision = {"close_ref": CLOSE_REF, "mode": "live",
                "parent_client_order_id": plan["parent_client_order_id"],
                "parent_order_id": plan["parent_order_id"],
                "reason": "L09の小額canary positionを一度だけ全量closeする。"}
    ledger = state / "receipts.jsonl"
    sealed = seal(ledger, decision, order)
    broker = read_live_close(credentials_path=credentials, cli_path=cli,
                             client_order_id=sealed["client_order_id"],
                             frozen_qty=plan["frozen_qty"], pre_usdc_qty=plan["pre_usdc_qty"],
                             buy_gross_cost_usdc=plan["buy_gross_cost_usdc"],
                             external_flow_fingerprint=plan["external_flow_fingerprint"])
    durable = effect_state(ledger, sealed["effect_id"])
    if broker["status"] in {"verified", "terminal_failure"}:
        outcome = ("live_close_verified" if broker["status"] == "verified"
                   else "live_close_terminal_failure")
        record_terminal_outcome(ledger, sealed, broker, outcome)
        _write_result(state / "live-close.json", broker)
        return _result(sealed, broker, False)
    if durable == "outcome":
        raise ValueError("live_close_outcome_readback_invalid")
    if durable == "started":
        return _result(sealed, broker, False)
    snapshot = read_live_close_snapshot(credentials_path=credentials, cli_path=cli)
    _gate(snapshot, plan, state)
    submitted = False
    if broker["status"] == "absent":
        with control_fence(state) as control:
            if control["paused"] or control["killed"]:
                raise ValueError("live_close_control_rejected")
            _gate(read_live_close_snapshot(credentials_path=credentials, cli_path=cli), plan, state)
            if mark_started(ledger, sealed):
                submitted = True
                submit_live_close(credentials_path=credentials, cli_path=cli,
                                  client_order_id=sealed["client_order_id"],
                                  frozen_qty=plan["frozen_qty"], order=order)
    for _ in range(10):
        broker = read_live_close(credentials_path=credentials, cli_path=cli,
                                 client_order_id=sealed["client_order_id"],
                                 frozen_qty=plan["frozen_qty"],
                                 pre_usdc_qty=plan["pre_usdc_qty"],
                                 buy_gross_cost_usdc=plan["buy_gross_cost_usdc"],
                                 external_flow_fingerprint=plan["external_flow_fingerprint"])
        if broker["status"] in {"verified", "terminal_failure", "partial_terminal"}:
            break
        time.sleep(1)
    if broker["status"] in {"verified", "terminal_failure"}:
        outcome = ("live_close_verified" if broker["status"] == "verified"
                   else "live_close_terminal_failure")
        record_terminal_outcome(ledger, sealed, broker, outcome)
        _write_result(state / "live-close.json", broker)
    return _result(sealed, broker, submitted)


if __name__ == "__main__":
    raise SystemExit(main())
