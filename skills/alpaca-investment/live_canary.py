#!/usr/bin/env python3
"""Execute or reconcile exactly one frozen $2 owner-capital live canary."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from allocator import build_candidates
from alpaca_cli import read_allocator_snapshot, read_live_canary, submit_live_canary
from control import control_fence, read_control
from effect_store import (effect_state, mark_started, record_terminal_outcome, seal,
                          unresolved_intent_count)
from risk_policy import evaluate_entry


CANARY_REF = "L09_LOCAL_CANARY_V1"
CLOUD_CANARY_REF = "L15_CLOUD_CANARY_V1"
ORDER = {"asset_class": "crypto", "notional_usd": "2.00", "side": "buy",
         "symbol": "BTC/USDC", "time_in_force": "gtc", "type": "market"}
DECISION = {"candidate_ref": "crypto://BTC/USDC", "canary_ref": CANARY_REF,
            "gate": "infrastructure_canary", "mode": "live",
            "reason": "最小実注文で一意注文・約定・照合経路を検証する。"}


def _paths() -> tuple[Path, Path, Path, str]:
    deployment = os.environ.get("LIFE_MANAGER_INVESTMENT_DEPLOYMENT")
    if os.environ.get("LIFE_MANAGER_INVESTMENT_MODE") != "live" \
            or deployment not in {"local", "cloud"}:
        raise ValueError("live_canary_context_invalid")
    credentials = os.environ.get("ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE")
    state = os.environ.get("ALPACA_INVESTMENT_LIVE_STATE_DIR")
    cli = os.environ.get("ALPACA_CLI", "~/.local/bin/alpaca")
    if not credentials or not state:
        raise ValueError("live_canary_context_invalid")
    return (Path(credentials).expanduser(), Path(state).expanduser(),
            Path(cli).expanduser(), deployment)


def _decision(deployment: str) -> dict:
    if deployment == "local":
        return DECISION
    return {**DECISION, "canary_ref": CLOUD_CANARY_REF, "deployment": deployment}


def _gate(snapshot: dict, state: Path) -> None:
    candidates = {row["candidate_ref"]: row for row in build_candidates(snapshot)}
    candidate = candidates.get("crypto://BTC/USDC")
    fixed = evaluate_entry(snapshot.get("risk"), "2.00")
    available = float(snapshot.get("available_cash_usd", "nan"))
    equity = float(snapshot.get("account", {}).get("equity", "nan"))
    if (not math.isfinite(available) or not math.isfinite(equity)
            or available < 0 or equity < 0
            or not candidate or not fixed["approved"] or snapshot.get("positions") != 0
            or snapshot.get("open_orders") != 0 or candidate["quote_age_seconds"] < 0
            or candidate["quote_age_seconds"] > 30 or candidate["spread_fraction"] > .15
            or snapshot.get("unresolved_intents") != 0
            or available - 2 < equity * .30):
        raise ValueError("live_canary_gate_rejected")
    control = read_control(state / "control.json")
    if control["paused"] or control["killed"]:
        raise ValueError("live_canary_control_rejected")


def _write_result(path: Path, value: dict) -> None:
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


def _write_cloud_ownership(state: Path, sealed: dict[str, str], result: dict | None = None) -> None:
    marker = {"entry_client_order_id": sealed["client_order_id"],
              "entry_effect_id": sealed["effect_id"], "entry_filled_qty": "0",
              "status": "entry_pending", "symbol": "BTCUSD"}
    if result and result.get("status") == "verified":
        try:
            filled = Decimal(str(result["order"]["filled_qty"]))
            owned = Decimal(str(result["position"]["qty"]))
            if (result["position"].get("symbol") != "BTCUSD" or not filled.is_finite()
                    or not owned.is_finite() or filled <= 0 or owned <= 0 or owned > filled):
                raise ValueError
        except (InvalidOperation, KeyError, TypeError, ValueError) as error:
            raise ValueError("live_canary_ownership_invalid") from error
        marker.update({"entry_filled_qty": str(filled), "owned_qty": str(owned),
                       "status": "open"})
    _write_result(state / "live-owned-position.json", marker)


def _output(sealed: dict[str, str], result: dict, submitted: bool, deployment: str) -> int:
    value = {"canary_ref": CLOUD_CANARY_REF if deployment == "cloud" else CANARY_REF,
             "deployment": deployment, "client_order_id": sealed["client_order_id"],
             "effect_id": sealed["effect_id"],
             "observed_at": datetime.now(timezone.utc).isoformat(),
             "status": result["status"], "submitted_this_run": submitted,
             "verified": result.get("verified") is True}
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0 if value["verified"] else 75


def main() -> int:
    credentials, state, cli, deployment = _paths()
    ledger = state / "receipts.jsonl"
    sealed = seal(ledger, _decision(deployment), ORDER)
    existing = read_live_canary(
        credentials_path=credentials, cli_path=cli, client_order_id=sealed["client_order_id"])
    durable = effect_state(ledger, sealed["effect_id"])
    if existing["status"] in {"verified", "terminal_failure"}:
        outcome = ("live_canary_verified" if existing["status"] == "verified"
                   else "live_canary_terminal_failure")
        record_terminal_outcome(ledger, sealed, existing, outcome)
        if deployment == "cloud" and existing["status"] == "verified":
            _write_cloud_ownership(state, sealed, existing)
        _write_result(state / "live-canary.json", existing)
        return _output(sealed, existing, False, deployment)
    if durable == "outcome":
        raise ValueError("live_canary_outcome_readback_invalid")
    if durable == "started":
        return _output(sealed, existing, False, deployment)

    snapshot = read_allocator_snapshot(
        credentials_path=credentials, cli_path=cli, risk_day_path=state / "risk-day.json")
    snapshot["unresolved_intents"] = unresolved_intent_count(ledger)
    _gate(snapshot, state)
    submitted = False
    if existing["status"] == "absent":
        with control_fence(state) as control:
            if control["paused"] or control["killed"]:
                raise ValueError("live_canary_control_rejected")
            if mark_started(ledger, sealed):
                submitted = True
                if deployment == "cloud":
                    _write_cloud_ownership(state, sealed)
                submit_live_canary(credentials_path=credentials, cli_path=cli,
                                   client_order_id=sealed["client_order_id"], order=ORDER)
    result = existing
    for _ in range(10):
        result = read_live_canary(
            credentials_path=credentials, cli_path=cli, client_order_id=sealed["client_order_id"])
        if result["status"] in {"verified", "terminal_failure"}:
            break
        time.sleep(1)
    if result["status"] in {"verified", "terminal_failure"}:
        outcome = ("live_canary_verified" if result["status"] == "verified"
                   else "live_canary_terminal_failure")
        record_terminal_outcome(ledger, sealed, result, outcome)
        if deployment == "cloud" and result["status"] == "verified":
            _write_cloud_ownership(state, sealed, result)
        _write_result(state / "live-canary.json", result)
    return _output(sealed, result, submitted, deployment)


if __name__ == "__main__":
    raise SystemExit(main())
