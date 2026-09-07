#!/usr/bin/env python3
"""Provider-neutral Reply/estimate lifecycle.

The model-facing ``decide`` callback owns conversation judgment. This kernel
owns only durable identity, intent fencing, official reconciliation and retry.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Protocol


MUTATIONS = frozenset({"reply", "estimate"})
NO_EFFECT = frozenset({"awaiting_buyer", "closed", "no_reply", "noop"})


class ReplyAdapter(Protocol):
    def observe_threads(self) -> list[dict[str, Any]]: ...
    def observe_one(self, thread_id: str) -> dict[str, Any]: ...
    def context(self, thread_id: str) -> dict[str, Any]: ...
    def mutate(self, intent: dict[str, Any]) -> None: ...
    def readback(self, intent: dict[str, Any]) -> dict[str, Any]: ...


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}_invalid")
    return value.strip()


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _observation(value: Mapping[str, Any]) -> dict[str, str]:
    fields = ("provider", "account_id", "thread_id", "latest_event_id", "observed_at")
    return {field: _text(value.get(field), field) for field in fields}


def _state_path(root: Path, row: Mapping[str, Any]) -> Path:
    identity = ":".join(row[field] for field in ("provider", "account_id", "thread_id"))
    return root / "threads" / hashlib.sha256(identity.encode()).hexdigest() / "state.json"


@contextmanager
def _lock(path: Path):
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
        raise ValueError("reply_state_invalid")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=".state-", delete=False, encoding="utf-8"
        ) as handle:
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


def _intent(row: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    action = _text(decision.get("action"), "action")
    if action not in MUTATIONS:
        raise ValueError("reply_action_invalid")
    payload = decision.get("payload")
    if not isinstance(payload, Mapping) or not payload:
        raise ValueError("reply_payload_invalid")
    base = {
        "version": 1,
        **{field: row[field] for field in (
            "provider", "account_id", "thread_id", "latest_event_id"
        )},
        "action": action,
        "payload": dict(payload),
        "content_sha256": _digest(payload),
    }
    return {**base, "effect_key": _digest(base)}


def _receipt(intent: Mapping[str, Any], readback: Mapping[str, Any]) -> dict[str, Any]:
    if readback.get("verified") is not True:
        raise ValueError("official_readback_unverified")
    return {
        "version": 1,
        "effect_key": intent["effect_key"],
        "provider_receipt_id": _text(
            readback.get("provider_receipt_id"), "provider_receipt_id"
        ),
        "observed_at": _text(readback.get("observed_at"), "observed_at"),
    }


def _pending(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "thread_id": row["thread_id"], "status": "pending", "reason": reason,
        "effect": 0, "readback": 0, "failed": 0,
    }


def _run_locked(
    adapter: ReplyAdapter,
    decide: Callable[[dict[str, Any]], Mapping[str, Any]],
    state_root: Path,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    row = _observation(source)
    path = _state_path(state_root, row)
    state = _load(path)
    retry_at = state.get("next_eligible_at")
    prior_observation = state.get("observation")
    if (isinstance(retry_at, str) and isinstance(prior_observation, Mapping)
            and prior_observation.get("latest_event_id") == row["latest_event_id"]):
        try:
            eligible = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("reply_retry_state_invalid") from None
        if datetime.now(timezone.utc) < eligible:
            return _pending(row, "retry_backoff")
    current = _observation(adapter.observe_one(row["thread_id"]))
    if any(current[field] != row[field] for field in ("provider", "account_id", "thread_id")):
        raise ValueError("reply_thread_identity_changed")
    row = current

    prior_intent = state.get("intent")
    prior_observation = state.get("observation")
    same_event = (
        isinstance(prior_observation, Mapping)
        and prior_observation.get("latest_event_id") == row["latest_event_id"]
    )
    if isinstance(prior_intent, Mapping) and same_event:
        official = adapter.readback(dict(prior_intent))
        if official.get("verified") is True:
            receipt = _receipt(prior_intent, official)
            _write(path, {"version": 1, "observation": row, "intent": prior_intent,
                          "receipt": receipt, "status": "verified"})
            return {"thread_id": row["thread_id"], "status": "verified",
                    "reason": "replay_zero", "effect": 0, "readback": 1, "failed": 0}
        if state.get("status") == "reconcile_unknown":
            return _pending(row, "reconcile_unknown")
        if official.get("authoritative_absent") is not True:
            return _pending(row, "reconcile_unknown")

    context = adapter.context(row["thread_id"])
    if not isinstance(context, Mapping):
        raise ValueError("reply_context_invalid")
    decision = decide({**row, "context": dict(context)})
    if not isinstance(decision, Mapping):
        raise ValueError("reply_decision_invalid")
    action = _text(decision.get("action"), "action")
    if action == "noop":
        classification = str(decision.get("classification") or "noop").strip()
        if classification not in NO_EFFECT:
            raise ValueError("reply_noop_classification_invalid")
        _write(path, {"version": 1, "observation": row, "status": classification})
        return {"thread_id": row["thread_id"], "status": classification,
                "reason": "no_effect_required", "effect": 0, "readback": 1, "failed": 0}
    if action in {"wait", "human"}:
        reason = _text(decision.get("reason"), "reason")
        remaining = decision.get("remaining_work")
        if not isinstance(remaining, list) or not remaining or not all(
            isinstance(item, str) and item.strip() for item in remaining
        ):
            raise ValueError("remaining_work_invalid")
        _write(path, {"version": 1, "observation": row,
                      "status": "waiting_human" if action == "human" else "waiting_external",
                      "blocker": reason, "remaining_work": remaining})
        return _pending(row, reason)

    intent = _intent(row, decision)
    _write(path, {"version": 1, "observation": row, "intent": intent,
                  "status": "intent_persisted"})
    refreshed = _observation(adapter.observe_one(row["thread_id"]))
    if refreshed["latest_event_id"] != row["latest_event_id"]:
        _write(path, {"version": 1, "observation": refreshed, "status": "context_stale"})
        return _pending(row, "newer_provider_event")
    existing = adapter.readback(intent)
    if existing.get("verified") is True:
        receipt = _receipt(intent, existing)
        _write(path, {"version": 1, "observation": refreshed, "intent": intent,
                      "receipt": receipt, "status": "verified"})
        return {"thread_id": row["thread_id"], "status": "verified",
                "reason": "reconciled", "effect": 0, "readback": 1, "failed": 0}
    if existing.get("authoritative_absent") is not True:
        _write(path, {"version": 1, "observation": refreshed, "intent": intent,
                      "status": "intent_persisted"})
        return _pending(row, "pre_effect_reconcile_unknown")
    adapter.mutate(intent)
    official = adapter.readback(intent)
    if official.get("verified") is not True:
        _write(path, {"version": 1, "observation": refreshed, "intent": intent,
                      "status": "reconcile_unknown"})
        return {"thread_id": row["thread_id"], "status": "pending",
                "reason": "reconcile_unknown", "effect": 1, "readback": 0, "failed": 0}
    receipt = _receipt(intent, official)
    _write(path, {"version": 1, "observation": refreshed, "intent": intent,
                  "receipt": receipt, "status": "verified"})
    return {"thread_id": row["thread_id"], "status": "verified",
            "reason": "submitted", "effect": 1, "readback": 1, "failed": 0}


def _run_one(adapter, decide, state_root, source):
    row = _observation(source)
    path = _state_path(state_root, row)
    with _lock(path):
        try:
            return _run_locked(adapter, decide, state_root, row)
        except Exception as error:
            state = _load(path)
            retry_count = min(int(state.get("retry_count", 0)) + 1, 10)
            delay = min(3600, 30 * (2 ** (retry_count - 1)))
            next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            _write(path, {
                "version": 1,
                "observation": row,
                "status": "retry_wait",
                "retry_count": retry_count,
                "next_eligible_at": next_at.isoformat().replace("+00:00", "Z"),
                "last_error": type(error).__name__,
            })
            return {"thread_id": row["thread_id"], "status": "failed",
                    "reason": type(error).__name__, "effect": 0,
                    "readback": 0, "failed": 1}


def run_wake(*, adapter: ReplyAdapter,
             decide: Callable[[dict[str, Any]], Mapping[str, Any]],
             state_root: Path, max_workers: int = 4) -> dict[str, Any]:
    try:
        rows = adapter.observe_threads()
        if not isinstance(rows, list):
            raise ValueError("reply_inventory_invalid")
        normalized = [_observation(row) for row in rows]
        identities = [(row["provider"], row["account_id"], row["thread_id"])
                      for row in normalized]
        if len(identities) != len(set(identities)):
            raise ValueError("reply_inventory_duplicate")
        workers = max(1, min(max_workers, len(normalized) or 1))
        if workers == 1:
            # Sync browser adapters are thread-affine: even a one-worker pool moves
            # their Playwright page to another thread and invalidates every call.
            items = [_run_one(adapter, decide, Path(state_root), row)
                     for row in normalized]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(_run_one, adapter, decide, Path(state_root), row)
                           for row in normalized]
                items = []
                for row, future in zip(normalized, futures):
                    try:
                        items.append(future.result())
                    except Exception as error:
                        items.append({"thread_id": row["thread_id"], "status": "failed",
                                      "reason": type(error).__name__, "effect": 0,
                                      "readback": 0, "failed": 1})
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            close()
    return {
        "status": "ok", "observed": len(items),
        "actionable": sum(item["status"] not in NO_EFFECT for item in items),
        "effect": sum(item["effect"] for item in items),
        "readback": sum(item["readback"] for item in items),
        "failed": sum(item["failed"] for item in items),
        "pending": sum(item["status"] == "pending" for item in items),
        "items": items,
    }


def _load_provider(path: Path, argv: list[str]):
    candidate = path.expanduser().resolve()
    if path.is_symlink() or not candidate.is_file():
        raise ValueError("reply_provider_adapter_invalid")
    name = "marketplace_reply_provider_" + hashlib.sha256(str(candidate).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, candidate)
    if spec is None or spec.loader is None:
        raise ValueError("reply_provider_adapter_invalid")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build = getattr(module, "build", None)
    if not callable(build):
        raise ValueError("reply_provider_adapter_invalid")
    built = build(argv)
    if not isinstance(built, tuple) or len(built) != 2 or not callable(built[1]):
        raise ValueError("reply_provider_adapter_invalid")
    adapter, decide = built
    for method in ("observe_threads", "observe_one", "context", "mutate", "readback"):
        if not callable(getattr(adapter, method, None)):
            raise ValueError("reply_provider_adapter_invalid")
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
    result = run_wake(adapter=adapter, decide=decide,
                      state_root=args.state_root.expanduser().resolve(),
                      max_workers=args.max_workers)
    _write(args.output.expanduser().resolve(), result)
    return int(result["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
