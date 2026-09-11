#!/usr/bin/env python3
"""Provider-neutral Paid wake orchestration.

The model-facing ``decide`` callback owns job judgment.  The kernel owns only
identity, durable intent, effect fencing, official reconciliation and receipts.
Provider adapters own observation, mutation and official readback mechanics.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Protocol


MUTATIONS = frozenset({"answer", "submit", "formal_delivery", "cancel"})
NO_EFFECT_CLASSIFICATIONS = frozenset({
    "completed", "awaiting_buyer", "reserved_for_owner", "satisfied_noop", "noop",
})


class PaidAdapter(Protocol):
    def observe_active(self) -> list[dict[str, Any]]: ...
    def observe_one(self, work_id: str) -> dict[str, Any]: ...
    def context(self, work_id: str) -> dict[str, Any]: ...
    def mutate(self, intent: dict[str, Any]) -> None: ...
    def readback(self, intent: dict[str, Any]) -> dict[str, Any]: ...


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_invalid")
    return value.strip()


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _item_key(row: Mapping[str, Any]) -> str:
    identity = ":".join(_text(row.get(field), field) for field in ("provider", "account_id", "work_id"))
    return hashlib.sha256(identity.encode()).hexdigest()


def _state_path(root: Path, row: Mapping[str, Any]) -> Path:
    return root / "items" / _item_key(row) / "state.json"


@contextmanager
def _item_lock(path: Path):
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("paid_state_invalid")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".state-", delete=False,
                                         encoding="utf-8") as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _observation(row: Mapping[str, Any]) -> dict[str, Any]:
    required = ("provider", "account_id", "work_id", "latest_event_id", "provider_state", "observed_at")
    return {field: _text(row.get(field), field) for field in required}


def _intent(row: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    action = _text(decision.get("action"), "action")
    if action not in MUTATIONS:
        raise ValueError("paid_action_invalid")
    payload = decision.get("payload")
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("paid_payload_invalid")
    content_sha256 = _digest(payload)
    base = {
        "version": 1,
        "provider": row["provider"],
        "account_id": row["account_id"],
        "work_id": row["work_id"],
        "latest_event_id": row["latest_event_id"],
        "action": action,
        "payload": dict(payload),
        "content_sha256": content_sha256,
    }
    return {**base, "effect_key": _digest(base)}


def _verified_receipt(intent: Mapping[str, Any], readback: Mapping[str, Any]) -> dict[str, Any]:
    if readback.get("verified") is not True:
        raise ValueError("official_readback_unverified")
    return {
        "version": 1,
        "effect_key": intent["effect_key"],
        "provider_receipt_id": _text(readback.get("provider_receipt_id"), "provider_receipt_id"),
        "observed_at": _text(readback.get("observed_at"), "observed_at"),
    }


def _pending(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {"work_id": row["work_id"], "status": "pending", "reason": reason,
            "effect": 0, "readback": 0, "failed": 0}


def _run_one_locked(adapter: PaidAdapter, decide: Callable[[dict[str, Any]], Mapping[str, Any]],
                    state_root: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    row = _observation(source)
    path = _state_path(state_root, row)
    state = _load(path)
    refreshed = _observation(adapter.observe_one(row["work_id"]))
    if any(refreshed[field] != row[field] for field in ("provider", "account_id", "work_id")):
        raise ValueError("paid_work_identity_changed")
    row = refreshed
    previous_intent = state.get("intent")
    previous_observation = state.get("observation")
    same_event = (isinstance(previous_observation, Mapping)
                  and previous_observation.get("latest_event_id") == row["latest_event_id"])
    if isinstance(previous_intent, Mapping) and same_event:
        official = adapter.readback(dict(previous_intent))
        if official.get("verified") is True:
            receipt = _verified_receipt(previous_intent, official)
            _write(path, {"version": 1, "observation": row, "intent": previous_intent,
                          "receipt": receipt, "status": "verified"})
            return {"work_id": row["work_id"], "status": "verified", "reason": "replay_zero",
                    "effect": 0, "readback": 1, "failed": 0}
        if official.get("authoritative_absent") is not True:
            return _pending(row, "reconcile_unknown")

    context = adapter.context(row["work_id"])
    if not isinstance(context, Mapping):
        raise ValueError("paid_context_invalid")
    decision = decide({**row, "context": dict(context)})
    if not isinstance(decision, Mapping):
        raise ValueError("paid_decision_invalid")
    action = _text(decision.get("action"), "action")
    if action == "noop":
        classification = str(decision.get("classification") or "noop").strip()
        if classification not in NO_EFFECT_CLASSIFICATIONS:
            raise ValueError("paid_noop_classification_invalid")
        _write(path, {"version": 1, "observation": row, "status": classification})
        return {"work_id": row["work_id"], "status": classification,
                "reason": "no_effect_required",
                "effect": 0, "readback": 1, "failed": 0}
    if action == "wait":
        reason = _text(decision.get("reason"), "reason")
        remaining = decision.get("remaining_work")
        if not isinstance(remaining, list) or not remaining or not all(isinstance(v, str) and v.strip() for v in remaining):
            raise ValueError("remaining_work_invalid")
        _write(path, {"version": 1, "observation": row, "status": "waiting_external",
                      "blocker": reason, "remaining_work": remaining})
        return _pending(row, reason)

    intent = _intent(row, decision)
    _write(path, {"version": 1, "observation": row, "intent": intent,
                  "status": "intent_persisted"})
    current = _observation(adapter.observe_one(row["work_id"]))
    if any(current[field] != row[field] for field in ("provider", "account_id", "work_id")):
        raise ValueError("paid_work_identity_changed")
    if current["latest_event_id"] != row["latest_event_id"]:
        _write(path, {"version": 1, "observation": current, "status": "context_stale"})
        return _pending(row, "newer_provider_event")
    existing = adapter.readback(intent)
    if existing.get("verified") is True:
        receipt = _verified_receipt(intent, existing)
        _write(path, {"version": 1, "observation": current, "intent": intent,
                      "receipt": receipt, "status": "verified"})
        return {"work_id": row["work_id"], "status": "verified", "reason": "reconciled",
                "effect": 0, "readback": 1, "failed": 0}
    adapter.mutate(intent)
    official = adapter.readback(intent)
    if official.get("verified") is not True:
        _write(path, {"version": 1, "observation": current, "intent": intent,
                      "status": "reconcile_unknown"})
        return {"work_id": row["work_id"], "status": "pending", "reason": "reconcile_unknown",
                "effect": 1, "readback": 0, "failed": 0}
    receipt = _verified_receipt(intent, official)
    _write(path, {"version": 1, "observation": current, "intent": intent,
                  "receipt": receipt, "status": "verified"})
    return {"work_id": row["work_id"], "status": "verified", "reason": "submitted",
            "effect": 1, "readback": 1, "failed": 0}


def _run_one(adapter: PaidAdapter, decide: Callable[[dict[str, Any]], Mapping[str, Any]],
             state_root: Path, source: Mapping[str, Any]) -> dict[str, Any]:
    row = _observation(source)
    with _item_lock(_state_path(state_root, row)):
        return _run_one_locked(adapter, decide, state_root, row)


def run_wake(*, adapter: PaidAdapter, decide: Callable[[dict[str, Any]], Mapping[str, Any]],
             state_root: Path, max_workers: int = 4) -> dict[str, Any]:
    rows = adapter.observe_active()
    if not isinstance(rows, list):
        raise ValueError("paid_inventory_invalid")
    normalized = [_observation(row) for row in rows]
    identities = [(row["provider"], row["account_id"], row["work_id"]) for row in normalized]
    if len(identities) != len(set(identities)):
        raise ValueError("paid_inventory_duplicate")
    workers = max(1, min(max_workers, len(normalized) or 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_one, adapter, decide, Path(state_root), row) for row in normalized]
        items = []
        for row, future in zip(normalized, futures):
            try:
                items.append(future.result())
            except Exception as error:
                error_detail = str(error).strip() or type(error).__name__
                items.append({"work_id": row["work_id"], "status": "failed",
                              "reason": type(error).__name__, "error_detail": error_detail,
                              "effect": 0,
                              "readback": 0, "failed": 1})
    return {
        "status": "ok",
        "observed": len(items),
        "actionable": sum(item["status"] not in NO_EFFECT_CLASSIFICATIONS for item in items),
        "effect": sum(item["effect"] for item in items),
        "readback": sum(item["readback"] for item in items),
        "failed": sum(item["failed"] for item in items),
        "pending": sum(item["status"] == "pending" for item in items),
        "items": items,
    }


def _load_provider(path: Path, argv: list[str]):
    candidate = path.expanduser().resolve()
    if path.is_symlink() or not candidate.is_file():
        raise ValueError("paid_provider_adapter_invalid")
    name = "marketplace_paid_provider_" + hashlib.sha256(str(candidate).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, candidate)
    if spec is None or spec.loader is None:
        raise ValueError("paid_provider_adapter_invalid")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build = getattr(module, "build", None)
    if not callable(build):
        raise ValueError("paid_provider_adapter_invalid")
    built = build(argv)
    if not isinstance(built, tuple) or len(built) != 2 or not callable(built[1]):
        raise ValueError("paid_provider_adapter_invalid")
    adapter, decide = built
    for method in ("observe_active", "observe_one", "context", "mutate", "readback"):
        if not callable(getattr(adapter, method, None)):
            raise ValueError("paid_provider_adapter_invalid")
    return adapter, decide


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-adapter", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-workers", type=int, default=4)
    args, provider_argv = parser.parse_known_args(argv)
    if provider_argv[:1] == ["--"]:
        provider_argv = provider_argv[1:]
    adapter, decide = _load_provider(args.provider_adapter, provider_argv)
    try:
        result = run_wake(adapter=adapter, decide=decide,
                          state_root=args.state_root.expanduser().resolve(),
                          max_workers=args.max_workers)
    except Exception as error:
        wait_reason = getattr(error, "paid_wait_reason", None)
        remaining = getattr(error, "paid_remaining_work", None)
        if (isinstance(wait_reason, str) and wait_reason.strip()
                and isinstance(remaining, list) and remaining
                and all(isinstance(value, str) and value.strip() for value in remaining)):
            result = {
                "status": "pending", "observed": 0, "actionable": 0,
                "effect": 0, "readback": 0, "failed": 0, "pending": 1,
                "items": [{"work_id": "__provider_inventory__", "status": "pending",
                           "reason": wait_reason.strip(), "remaining_work": remaining,
                           "effect": 0, "readback": 0, "failed": 0}],
            }
        else:
            result = {
                "status": "failed", "observed": 0, "actionable": 0, "effect": 0, "readback": 0,
                "failed": 1, "pending": 0, "failed_step": "provider_inventory",
                "error_type": type(error).__name__, "items": [],
            }
    _write(args.output.expanduser().resolve(), result)
    return int(result["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
