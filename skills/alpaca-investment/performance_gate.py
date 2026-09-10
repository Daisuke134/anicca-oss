#!/usr/bin/env python3
"""Materialize the local live-account performance gate from official receipts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from alpaca_cli import read_live_performance_snapshot
from performance import project


def _context() -> tuple[Path, Path, Path]:
    if (os.environ.get("LIFE_MANAGER_INVESTMENT_MODE") != "live"
            or os.environ.get("LIFE_MANAGER_INVESTMENT_DEPLOYMENT") != "local"):
        raise ValueError("live_performance_context_invalid")
    credentials = os.environ.get("ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE")
    state = os.environ.get("ALPACA_INVESTMENT_LIVE_STATE_DIR")
    if not credentials or not state:
        raise ValueError("live_performance_context_invalid")
    return (Path(credentials).expanduser(), Path(state).expanduser(),
            Path(os.environ.get("ALPACA_CLI", "~/.local/bin/alpaca")).expanduser())


def _round_trip(ledger: Path) -> tuple[str, str, str]:
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]
    canary = [row for row in rows if row.get("receipt_type") == "decision"
              and row.get("decision", {}).get("canary_ref") == "L09_LOCAL_CANARY_V1"]
    close = [row for row in rows if row.get("receipt_type") == "decision"
             and row.get("decision", {}).get("close_ref") == "L10_LOCAL_CLOSE_V1"]
    if len(canary) != 1 or len(close) != 1:
        raise ValueError("live_performance_round_trip_invalid")

    def client(decision: dict) -> str:
        matches = [row.get("client_order_id") for row in rows
                   if row.get("receipt_type") == "effect_intent"
                   and row.get("decision_id") == decision.get("decision_id")
                   and row.get("status") == "planned"]
        if len(matches) != 1 or not isinstance(matches[0], str):
            raise ValueError("live_performance_round_trip_invalid")
        return matches[0]

    return canary[0]["recorded_at"], client(canary[0]), client(close[0])


def _write(path: Path, value: dict) -> None:
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


def main() -> int:
    credentials, state, cli = _context()
    period_start, buy_client, sell_client = _round_trip(state / "receipts.jsonl")
    snapshot = read_live_performance_snapshot(
        credentials_path=credentials, cli_path=cli, period_start=period_start,
        buy_client_order_id=buy_client, sell_client_order_id=sell_client)
    result = project(snapshot)
    if result.get("measurement_status") != "measured":
        raise ValueError("live_performance_projection_blocked")
    _write(state / "performance-latest.json", result)
    print(json.dumps({key: result[key] for key in (
        "measurement_status", "net_pnl_usd", "realized_pnl_usd", "unrealized_pnl_usd",
        "fees_usd", "slippage_usd", "max_drawdown_usd", "gross_exposure_usd",
        "benchmark_return", "alpha_pnl_usd", "completed_round_trips",
        "statistically_supported", "capital_cap_usd", "capital_expansion_allowed", "reason",
    )}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
