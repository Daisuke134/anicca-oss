#!/usr/bin/env python3
"""Mercor boundary for the shared marketplace Paid kernel."""
from __future__ import annotations
import argparse, json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

ACTIVE_STATES = frozenset({"selected", "contracted", "authorized_work", "work_submitted", "needs_human"})
TERMINAL_STATES = frozenset({"accepted", "paid_settled", "bank_matched", "revenue_recorded", "rejected"})

class MercorPaidWait(RuntimeError):
    def __init__(self, reason: str, remaining_work: list[str]):
        super().__init__(reason); self.paid_wait_reason = reason; self.paid_remaining_work = remaining_work

def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise MercorPaidWait("official_work_inventory_unavailable", ["resume the official Mercor observer and obtain an identity-bound work event"])
    rows = []
    try: lines = path.read_text(encoding="utf-8").splitlines()
    except OSError: raise RuntimeError("mercor_paid_inventory_unavailable") from None
    for line in lines:
        try: value = json.loads(line)
        except ValueError: raise RuntimeError("mercor_paid_inventory_unavailable") from None
        required = ("work_id", "event_id", "state", "evidence_ref", "observed_at")
        if not isinstance(value, Mapping) or any(not isinstance(value.get(field), str) or not value[field].strip() for field in required):
            raise RuntimeError("mercor_paid_inventory_unavailable")
        if value["state"] not in ACTIVE_STATES | TERMINAL_STATES: raise RuntimeError("mercor_paid_inventory_unavailable")
        evidence = urlparse(value["evidence_ref"])
        if evidence.scheme != "https" or evidence.hostname != "work.mercor.com":
            raise MercorPaidWait(
                "official_work_receipt_required",
                ["observe this work item on work.mercor.com and persist its identity-bound official URL"],
            )
        rows.append(dict(value))
    return rows

class MercorPaidAdapter:
    def __init__(self, *, account_id: str, work_events: Path):
        if not isinstance(account_id, str) or not account_id.strip(): raise ValueError("mercor_account_id_invalid")
        self.account_id = account_id.strip(); self.work_events = work_events.expanduser().resolve(); self._contexts = {}
    def _inventory(self) -> list[dict[str, Any]]:
        latest, histories = {}, {}
        for row in _rows(self.work_events):
            work_id = row["work_id"].strip(); histories.setdefault(work_id, []).append(row); latest[work_id] = row
        self._contexts = {key: {"events": value, "latest": latest[key]} for key, value in histories.items()}
        return [{"provider":"mercor", "account_id":self.account_id, "work_id":work_id,
                 "latest_event_id":row["event_id"], "provider_state":row["state"], "observed_at":row["observed_at"]}
                for work_id, row in latest.items() if row["state"] in ACTIVE_STATES]
    def observe_active(self): return self._inventory()
    def observe_one(self, work_id: str):
        matches = [row for row in self._inventory() if row["work_id"] == work_id]
        if len(matches) != 1: raise RuntimeError("mercor_paid_work_unavailable")
        return matches[0]
    def context(self, work_id: str):
        if work_id not in self._contexts: self._inventory()
        try: return dict(self._contexts[work_id])
        except KeyError: raise RuntimeError("mercor_paid_work_unavailable") from None
    def mutate(self, intent): raise RuntimeError("mercor_human_submission_required")
    def readback(self, intent): return {"verified":False, "authoritative_absent":False}

def decide(row: Mapping[str, Any]) -> dict[str, Any]:
    state = row.get("provider_state")
    if state == "work_submitted": return {"action":"noop", "classification":"awaiting_buyer"}
    if state == "needs_human": return {"action":"wait", "reason":"mercor_human_action_required", "remaining_work":["complete the identity-bound task and resume this exact work item"]}
    if state in {"selected", "contracted"}: return {"action":"wait", "reason":"mercor_work_authorization_required", "remaining_work":["read the official contract and record explicit AI work authorization"]}
    if state == "authorized_work": return {"action":"wait", "reason":"mercor_human_submission_required", "remaining_work":["prepare the artifact and obtain the required human submission receipt"]}
    raise RuntimeError("mercor_paid_state_unavailable")

def build(argv: list[str]):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--account-id",required=True); parser.add_argument("--work-events",required=True,type=Path); args=parser.parse_args(argv)
    return MercorPaidAdapter(account_id=args.account_id,work_events=args.work_events), decide

__all__=["MercorPaidAdapter","MercorPaidWait","build","decide"]
