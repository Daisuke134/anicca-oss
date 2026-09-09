#!/usr/bin/env python3
"""Thin Lancers boundary for the shared marketplace Paid kernel."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Callable, Mapping


HERE = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / ".local/state/anicca/lancers/application.json"


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_work_sync():
    path = HERE / "work_sync.py"
    spec = importlib.util.spec_from_file_location("lancers_paid_work_sync", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("lancers_paid_inventory_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LancersPaidAdapter:
    def __init__(self, *, account_id: str, inventory_reader: Callable[[], Mapping[str, Any]],
                 clock: Callable[[], str] | None = None):
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("lancers_account_id_invalid")
        self.account_id = account_id.strip()
        self.inventory_reader = inventory_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        self._contexts: dict[str, dict[str, Any]] = {}

    def _inventory(self) -> list[dict[str, Any]]:
        snapshot = self.inventory_reader()
        if (not isinstance(snapshot, Mapping) or snapshot.get("ok") is not True
                or snapshot.get("source_complete") is not True
                or not isinstance(snapshot.get("contract_candidates"), list)):
            raise RuntimeError("lancers_paid_inventory_unavailable")
        observed_at = self.clock()
        rows = []
        contexts: dict[str, dict[str, Any]] = {}
        for candidate in snapshot["contract_candidates"]:
            if not isinstance(candidate, Mapping):
                raise RuntimeError("lancers_paid_inventory_unavailable")
            kind = candidate.get("source_kind")
            provider_id = candidate.get("provider_id")
            funding = candidate.get("funding_status")
            if kind not in {"project", "monthly", "storefront"} or not isinstance(provider_id, str) or not provider_id:
                raise RuntimeError("lancers_paid_inventory_unavailable")
            if not isinstance(funding, str) or not funding:
                raise RuntimeError("lancers_paid_inventory_unavailable")
            work_id = f"{kind}:{provider_id}"
            event = _digest({"contract": candidate, "boards": snapshot.get("boards", [])})
            rows.append({
                "provider": "lancers", "account_id": self.account_id, "work_id": work_id,
                "latest_event_id": event, "provider_state": funding, "observed_at": observed_at,
            })
            contexts[work_id] = {
                "contract": dict(candidate),
                "boards": list(snapshot.get("boards", [])),
                "finance": dict(snapshot.get("finance", {})),
            }
        self._contexts = contexts
        return rows

    def observe_active(self) -> list[dict[str, Any]]:
        return self._inventory()

    def observe_one(self, work_id: str) -> dict[str, Any]:
        matches = [row for row in self._inventory() if row["work_id"] == work_id]
        if len(matches) != 1:
            raise RuntimeError("lancers_paid_work_unavailable")
        return matches[0]

    def context(self, work_id: str) -> dict[str, Any]:
        if work_id not in self._contexts:
            self._inventory()
        try:
            return dict(self._contexts[work_id])
        except KeyError:
            raise RuntimeError("lancers_paid_work_unavailable") from None

    def mutate(self, intent: dict[str, Any]) -> None:
        raise RuntimeError("lancers_paid_effect_not_implemented")

    def readback(self, intent: dict[str, Any]) -> dict[str, Any]:
        return {"verified": False, "authoritative_absent": False}


def decide(row: Mapping[str, Any]) -> dict[str, Any]:
    if row.get("provider_state") != "requires_detail_readback":
        raise RuntimeError("lancers_paid_planner_required")
    return {
        "action": "wait",
        "reason": "official_contract_detail_required",
        "remaining_work": ["read funded contract terms and complete buyer context"],
    }


def build(argv: list[str]):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)
    work_sync = _load_work_sync()
    reader = lambda: work_sync.read_paid_inventory(
        state_path=args.state_path.expanduser().resolve()
    )
    return LancersPaidAdapter(account_id=args.account_id, inventory_reader=reader), decide


__all__ = ["LancersPaidAdapter", "build", "decide"]
