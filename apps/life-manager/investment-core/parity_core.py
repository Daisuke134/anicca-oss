"""Pure host-neutral Investment parity core for sealed inputs."""

from __future__ import annotations

import hashlib
import json


def _digest(value: dict) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run_parity_core(fixture: dict) -> dict:
    decision = fixture.get("no_trade") if isinstance(fixture, dict) else None
    account = fixture.get("observation", {}).get("account", {}) if isinstance(fixture, dict) else {}
    if not isinstance(decision, dict) or decision.get("candidate_ref") != "NO_TRADE":
        raise ValueError("investment_parity_input_invalid")
    if decision.get("approved") is not False or decision.get("gate") != "model_no_trade":
        raise ValueError("investment_parity_input_invalid")
    values = (decision.get("reason"), decision.get("observed_at"), account.get("cash"), account.get("equity"))
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("investment_parity_input_invalid")
    core = {
        "decision": "NO_TRADE",
        "report": {"cash": account["cash"], "equity": account["equity"], "reason": decision["reason"]},
        "risk": {"approved": False, "effect_permission": "none", "gate": "model_no_trade"},
    }
    core_digest = _digest(core)
    return {**core, "core_digest": core_digest,
            "idempotency_key": hashlib.sha256(
                f"investment-parity\n{decision['observed_at']}\n{core_digest}".encode("utf-8")
            ).hexdigest()}
