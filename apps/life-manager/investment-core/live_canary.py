#!/usr/bin/env python3
"""Execute or reconcile exactly one frozen $2 owner-capital live canary."""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from allocator import build_candidates
from alpaca_cli import read_allocator_snapshot, read_live_canary, submit_live_canary
from control import control_fence, read_control
from effect_store import (effect_state, mark_started, record_terminal_outcome, seal,
                          unresolved_intent_count)
from risk_policy import evaluate_entry


CANARY_REF = "L09_LOCAL_CANARY_V1"
ORDER = {"asset_class": "crypto", "notional_usd": "2.00", "side": "buy",
         "symbol": "BTC/USDC", "time_in_force": "gtc", "type": "market"}
DECISION = {"candidate_ref": "crypto://BTC/USDC", "canary_ref": CANARY_REF,
            "gate": "infrastructure_canary", "mode": "live",
            "reason": "最小実注文で一意注文・約定・照合経路を検証する。"}


def _paths() -> tuple[Path, Path, Path]:
    if os.environ.get("LIFE_MANAGER_INVESTMENT_MODE") != "live" \
            or os.environ.get("LIFE_MANAGER_INVESTMENT_DEPLOYMENT") != "local":
        raise ValueError("live_canary_context_invalid")
    credentials = os.environ.get("ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE")
    state = os.environ.get("ALPACA_INVESTMENT_LIVE_STATE_DIR")
    cli = os.environ.get("ALPACA_CLI", "~/.local/bin/alpaca")
    if not credentials or not state:
        raise ValueError("live_canary_context_invalid")
    return Path(credentials).expanduser(), Path(state).expanduser(), Path(cli).expanduser()


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


def _output(sealed: dict[str, str], result: dict, submitted: bool) -> int:
    value = {"canary_ref": CANARY_REF, "client_order_id": sealed["client_order_id"],
             "effect_id": sealed["effect_id"],
             "observed_at": datetime.now(timezone.utc).isoformat(),
             "status": result["status"], "submitted_this_run": submitted,
             "verified": result.get("verified") is True}
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0 if value["verified"] else 75


def main() -> int:
    credentials, state, cli = _paths()
    ledger = state / "receipts.jsonl"
    sealed = seal(ledger, DECISION, ORDER)
    existing = read_live_canary(
        credentials_path=credentials, cli_path=cli, client_order_id=sealed["client_order_id"])
    durable = effect_state(ledger, sealed["effect_id"])
    if existing["status"] in {"verified", "terminal_failure"}:
        outcome = ("live_canary_verified" if existing["status"] == "verified"
                   else "live_canary_terminal_failure")
        record_terminal_outcome(ledger, sealed, existing, outcome)
        _write_result(state / "live-canary.json", existing)
        return _output(sealed, existing, False)
    if durable == "outcome":
        raise ValueError("live_canary_outcome_readback_invalid")
    if durable == "started":
        return _output(sealed, existing, False)

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
        _write_result(state / "live-canary.json", result)
    return _output(sealed, result, submitted)


if __name__ == "__main__":
    raise SystemExit(main())
