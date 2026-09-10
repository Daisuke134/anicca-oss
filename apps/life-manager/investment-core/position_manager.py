"""Model HOLD/EXIT judgment with a deterministic live close boundary."""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def choose(snapshot: dict[str, Any], observation: dict[str, Any], state: Path,
           runner: Path, workdir: Path) -> dict[str, Any]:
    positions = [row for row in observation.get("positions", [])
                 if row.get("symbol") != "USDCUSD"]
    try:
        ownership = json.loads((state / "live-owned-position.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise ValueError("live_position_not_owned") from error
    if (ownership.get("status") != "open" or ownership.get("symbol") != "BTCUSD"
            or not isinstance(ownership.get("entry_client_order_id"), str)
            or not ownership["entry_client_order_id"].startswith("lm-ai-")):
        raise ValueError("live_position_not_owned")
    if len(positions) != 1 or positions[0].get("symbol") != "BTCUSD" \
            or snapshot.get("open_orders") != 0 or snapshot.get("unresolved_intents") != 0:
        raise ValueError("live_position_gate_rejected")
    try:
        qty = Decimal(str(positions[0]["qty"]))
        allocated = Decimal(str(snapshot["risk"]["allocated_capital_usd"]))
        if (not qty.is_finite() or qty <= 0 or qty.as_tuple().exponent < -9
                or not allocated.is_finite() or allocated <= 0 or allocated > Decimal("100")):
            raise ValueError
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("live_position_gate_rejected") from error
    schema = state / "position-decision-schema.json"
    evidence = state / "position-agent-evidence"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    schema.write_text(json.dumps({"type": "object", "additionalProperties": False,
        "properties": {"action": {"enum": ["HOLD", "EXIT"]}, "reason": {"type": "string"}},
        "required": ["action", "reason"]}), encoding="utf-8")
    prompt = ("You manage one real-money BTC position. Choose exactly HOLD or EXIT from only the "
              "official snapshot. Never invent market data. Prefer EXIT when expected value of "
              "remaining invested is not positive. Write one concise Japanese reason.\n" +
              json.dumps({"position": positions[0], "quotes": snapshot.get("crypto"),
                          "history_5min": snapshot.get("crypto_history", {}).get("BTC/USDC", []),
                          "risk": snapshot.get("risk")}, separators=(",", ":")))
    result = subprocess.run([str(runner), "--task-class", "diagnostic-agent", "--prompt-stdin",
        "--schema", str(schema), "--evidence-dir", str(evidence), "--task-label",
        "alpaca-position", "--loop", "alpaca-investment", "--workdir", str(workdir),
        "--timeout-seconds", "120", "--read-only"], input=prompt, text=True,
        capture_output=True, timeout=135, check=False)
    if result.returncode != 0:
        raise ValueError("position_agent_failed")
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    decision = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    if decision.get("action") not in {"HOLD", "EXIT"} or not isinstance(decision.get("reason"), str):
        raise ValueError("position_agent_invalid")
    return {**decision, "qty": str(qty)}


def exit_order(decision: dict[str, Any]) -> dict[str, Any]:
    return {"asset_class": "crypto", "qty": decision["qty"], "side": "sell",
            "symbol": "BTC/USDC", "time_in_force": "gtc", "type": "market"}
