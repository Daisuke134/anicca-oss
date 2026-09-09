"""Durable New-York-day loss accounting for the investment loop."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


_STATUSES = {"QUEUED", "PENDING", "PROCESSING", "COMPLETE", "FAILED", "CANCELED"}
_DIRECTIONS = {"INCOMING", "OUTGOING"}


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError from error
    if not number.is_finite():
        raise ValueError
    return number


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _transfers(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise ValueError
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError
        transfer_id = row.get("id")
        asset = row.get("asset")
        direction = row.get("direction")
        status = row.get("status")
        if (not isinstance(transfer_id, str) or not transfer_id
                or not isinstance(asset, str) or not asset
                or direction not in _DIRECTIONS or status not in _STATUSES
                or transfer_id in result):
            raise ValueError
        normalized = {"asset": asset, "direction": direction, "status": status,
                      "accounted": status == "COMPLETE"}
        if status == "COMPLETE":
            usd_value = _decimal(row.get("usd_value"))
            if usd_value < 0:
                raise ValueError
            normalized["usd_value"] = str(usd_value)
        result[transfer_id] = normalized
    return result


def reconcile(path: Path, *, observed_at: datetime, equity: Any, bank_cash_flow: Any,
              transfers: Any, official_pnl: Any = None) -> dict[str, Any]:
    """Persist a daily baseline and return conservative loss inputs.

    A first observation establishes the baseline and is deliberately not entry-ready.
    Crypto cash flow is recognized when COMPLETE is first observed, not by created_at.
    """
    if observed_at.tzinfo is None:
        raise ValueError("risk_day_invalid")
    ny_day = observed_at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    current_equity = _decimal(equity)
    bank_flow = _decimal(bank_cash_flow)
    current = _transfers(transfers)
    state = None
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("risk_day_invalid") from error
    if not isinstance(state, dict) or state.get("ny_day") != ny_day:
        state = {"ny_day": ny_day, "baseline_equity": str(current_equity),
                 "baseline_observed_at": observed_at.isoformat(), "crypto_cash_flow": "0",
                 "baseline_bank_cash_flow": str(bank_flow),
                 "transfers": current}
        _atomic(path, state)
        return {"cash_flow_ny_day_usd": "0",
                "equity_pnl_ny_day_usd": "0", "official_pnl_ny_day_usd": None,
                "risk_day_ready": False}
    try:
        baseline = _decimal(state["baseline_equity"])
        baseline_bank_flow = _decimal(state["baseline_bank_cash_flow"])
        crypto_flow = _decimal(state["crypto_cash_flow"])
        previous = state["transfers"]
        if not isinstance(previous, dict) or not set(previous).issubset(current):
            raise ValueError
        for transfer_id, before in previous.items():
            now = current[transfer_id]
            if (before.get("asset"), before.get("direction")) != (now["asset"], now["direction"]):
                raise ValueError
            if before.get("status") == "COMPLETE" and now["status"] != "COMPLETE":
                raise ValueError
        for transfer_id, now in current.items():
            before = previous.get(transfer_id)
            already = bool(before and before.get("accounted"))
            if now["status"] == "COMPLETE" and not already:
                value = _decimal(now["usd_value"])
                crypto_flow += value if now["direction"] == "INCOMING" else -value
            now["accounted"] = already or now["status"] == "COMPLETE"
        official = None if official_pnl is None else _decimal(official_pnl)
        total_flow = bank_flow - baseline_bank_flow + crypto_flow
        equity_pnl = current_equity - baseline - total_flow
        state.update({"crypto_cash_flow": str(crypto_flow), "transfers": current})
        _atomic(path, state)
        return {"cash_flow_ny_day_usd": str(total_flow),
                "equity_pnl_ny_day_usd": str(equity_pnl),
                "official_pnl_ny_day_usd": None if official is None else str(official),
                "risk_day_ready": official is not None}
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("risk_day_invalid") from error
