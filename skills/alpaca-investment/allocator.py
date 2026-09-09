"""Model allocation with a deterministic paper-risk boundary."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from risk_policy import evaluate_entry


MAX_QUOTE_AGE_SECONDS = 30
MAX_SPREAD_FRACTION = .15
MIN_CASH_FRACTION = .30


def _age_seconds(timestamp: str) -> float:
    normalized = timestamp.replace("Z", "+00:00")
    # Apple system Python 3.9 accepts at most six fractional-second digits;
    # Alpaca can return nanosecond timestamps. Keep the instant, normalize the
    # precision, and make the same candidate gate portable across host Pythons.
    match = re.fullmatch(r"(.*)\.(\d+)([+-]\d{2}:\d{2})", normalized)
    if match:
        normalized = f"{match.group(1)}.{match.group(2)[:6].ljust(6, '0')}{match.group(3)}"
    observed = datetime.fromisoformat(normalized)
    return (datetime.now(timezone.utc) - observed).total_seconds()


def _option_parts(symbol: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"SPY(\d{6})C(\d{8})", symbol)
    return (match.group(1), int(match.group(2))) if match else None


def build_candidates(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for quote in snapshot["crypto"]:
        bid, ask = float(quote["bid"]), float(quote["ask"])
        if bid > 0 and ask >= bid:
            candidates.append({
                "asset_class": "crypto", "ask": ask, "bid": bid,
                "candidate_ref": f"crypto://{quote['symbol']}",
                "max_loss_usd": 10.0, "quote_age_seconds": _age_seconds(quote["quote_at"]),
                "spread_fraction": (ask - bid) / ask, "symbol": quote["symbol"],
            })
    asset, quote = snapshot["qqq_asset"], snapshot["qqq_quote"]
    if (asset.get("tradable") is True and asset.get("status") == "active"
            and asset.get("overnight_tradable") is True
            and asset.get("overnight_halted") is False):
        bid, ask = float(quote["bid"]), float(quote["ask"])
        candidates.append({
            "asset_class": "equity", "ask": ask, "bid": bid,
            "candidate_ref": "equity://QQQ", "max_loss_usd": 10.0,
            "quote_age_seconds": _age_seconds(quote["quote_at"]),
            "spread_fraction": (ask - bid) / ask, "symbol": "QQQ",
        })
    if snapshot["clock"].get("is_open") is True:
        quotes = sorted(snapshot["option_quotes"], key=lambda row: row["symbol"])
        for left, right in zip(quotes, quotes[1:]):
            a, b = _option_parts(left["symbol"]), _option_parts(right["symbol"])
            if not a or not b or a[0] != b[0] or b[1] - a[1] != 1000:
                continue
            debit = float(left["ask"]) - float(right["bid"])
            if debit <= 0:
                continue
            candidates.append({
                "asset_class": "option_spread",
                "ask": float(left["ask"]), "bid": float(right["bid"]),
                "candidate_ref": f"option-spread://{left['symbol']}-{right['symbol']}",
                "long_symbol": left["symbol"], "short_symbol": right["symbol"],
                "max_loss_usd": round(debit * 100, 2),
                "max_profit_usd": round((1 - debit) * 100, 2),
                "quote_age_seconds": max(_age_seconds(left["quote_at"]), _age_seconds(right["quote_at"])),
                "spread_fraction": abs(float(left["ask"]) - float(left["bid"])) / float(left["ask"]),
            })
    return candidates


def _schema(path: Path) -> None:
    schema = {"type": "object", "additionalProperties": False,
              "properties": {
                  "candidate_ref": {"type": "string"}, "probability_profit": {"type": "number"},
                  "expected_gain_usd": {"type": "number"}, "reason": {"type": "string"}},
              "required": ["candidate_ref", "probability_profit", "expected_gain_usd", "reason"]}
    path.write_text(json.dumps(schema), encoding="utf-8")


def choose(snapshot: dict[str, Any], candidates: list[dict[str, Any]], state: Path,
           runner: Path, workdir: Path) -> dict[str, Any]:
    schema_path, evidence = state / "decision-schema.json", state / "agent-evidence"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    _schema(schema_path)
    prompt = (
        "You allocate a paper-only investment account. Select exactly one candidate_ref offered below, "
        "or NO_TRADE. Judge near-term expected value from only this snapshot; never invent market data. "
        "probability_profit must be 0..1 and expected_gain_usd must be the upside conditional on profit. "
        "Write reason as one concise natural Japanese sentence. "
        "Choose NO_TRADE with both numbers 0 when evidence is inadequate.\n"
        + json.dumps({"account": snapshot["account"], "candidates": candidates}, separators=(",", ":"))
    )
    result = subprocess.run([
        str(runner), "--task-class", "diagnostic-agent", "--prompt-stdin",
        "--schema", str(schema_path), "--evidence-dir", str(evidence),
        "--task-label", "alpaca-allocation", "--loop", "alpaca-investment",
        "--workdir", str(workdir), "--timeout-seconds", "120", "--read-only",
    ], input=prompt, text=True, capture_output=True, timeout=135, check=False)
    if result.returncode != 0:
        raise ValueError("allocation_agent_failed")
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    decision = json.loads(Path(summary["result_path"]).read_text(encoding="utf-8"))
    gated = gate(snapshot, candidates, decision)
    gated["observed_at"] = snapshot["clock"]["timestamp"]
    return gated


def gate(snapshot: dict[str, Any], candidates: list[dict[str, Any]], decision: dict[str, Any]) -> dict[str, Any]:
    offered = {row["candidate_ref"]: row for row in candidates}
    ref = decision.get("candidate_ref")
    if ref == "NO_TRADE":
        return {**decision, "approved": False, "gate": "model_no_trade"}
    candidate = offered.get(ref)
    if candidate is None:
        return {**decision, "approved": False, "gate": "candidate_not_offered"}
    equity, cash = float(snapshot["account"]["equity"]), float(snapshot["account"]["cash"])
    probability, gain = float(decision["probability_profit"]), float(decision["expected_gain_usd"])
    loss = float(candidate["max_loss_usd"])
    fixed_risk = evaluate_entry(snapshot.get("risk"), candidate["max_loss_usd"])
    checks = {
        "quote_fresh": 0 <= candidate["quote_age_seconds"] <= MAX_QUOTE_AGE_SECONDS,
        "spread": candidate["spread_fraction"] <= MAX_SPREAD_FRACTION,
        "expected_value": 0 <= probability <= 1 and gain > 0 and probability * gain - (1 - probability) * loss > 0,
        "fixed_risk": fixed_risk["approved"],
        "cash_reserve": cash - loss >= equity * MIN_CASH_FRACTION,
        "position_slot": snapshot["positions"] == 0,
        "order_slot": snapshot["open_orders"] == 0,
        "intent_slot": snapshot.get("unresolved_intents") == 0,
    }
    if candidate["asset_class"] == "option_spread":
        checks["bounded_upside"] = gain <= float(candidate["max_profit_usd"])
        checks["regular_session"] = snapshot["clock"].get("is_open") is True
    approved = all(checks.values()) and candidate["asset_class"] in {"crypto", "option_spread"}
    return {**decision, "approved": approved, "candidate": candidate,
            "checks": checks, "fixed_risk": fixed_risk,
            "gate": "approved" if approved else "risk_rejected"}


def order_for(decision: dict[str, Any]) -> dict[str, Any]:
    candidate = decision["candidate"]
    if candidate["asset_class"] == "crypto":
        return {"asset_class": "crypto", "notional_usd": f"{candidate['max_loss_usd']:.2f}",
                "side": "buy", "symbol": candidate["symbol"], "time_in_force": "gtc", "type": "market"}
    return {"asset_class": "option_spread", "limit_price": f"{candidate['max_loss_usd'] / 100:.2f}",
            "long_symbol": candidate["long_symbol"], "short_symbol": candidate["short_symbol"],
            "time_in_force": "day", "type": "limit"}
