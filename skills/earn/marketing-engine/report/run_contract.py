#!/usr/bin/env python3
"""Truthful, append-only final-run records for Marketing Engine runners."""

from __future__ import annotations

import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import pathlib
import re
from typing import Callable


RUNNERS = frozenset({
    "mine", "score", "metrics", "dashboard", "clip",
    "self-improve", "capafy",
})
STATUSES = frozenset({"success", "partial", "failed", "skipped"})
ENVIRONMENTS = frozenset({"production", "test"})
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REQUIRED = frozenset({
    "schema_version", "run_id", "runner_id", "environment", "started_at",
    "finished_at", "status", "dry_run", "product_ids", "effects",
    "metrics", "evidence", "error",
})


class ContractError(ValueError):
    pass


class ConflictError(ContractError):
    pass


@dataclasses.dataclass(frozen=True)
class RecordResult:
    event: dict
    created: bool


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ContractError(message)


def _timestamp(value: object, field: str) -> dt.datetime:
    _require(isinstance(value, str) and value, f"{field} must be an RFC3339 timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} must be an RFC3339 timestamp") from exc
    _require(parsed.tzinfo is not None, f"{field} must include a timezone")
    return parsed


def validate_event(event: dict) -> dict:
    """Validate and return a detached event suitable for canonical persistence."""
    _require(isinstance(event, dict), "event must be an object")
    missing = sorted(REQUIRED - set(event))
    _require(not missing, f"missing required fields: {', '.join(missing)}")
    out = json.loads(json.dumps(event, ensure_ascii=False))

    _require(out["schema_version"] == "marketing.run.v1", "unsupported schema_version")
    _require(isinstance(out["run_id"], str) and RUN_ID_RE.fullmatch(out["run_id"]) is not None,
             "run_id must be 32 lowercase hex characters")
    _require(out["run_id"] != "0" * 32, "run_id must be non-zero")
    _require(out["runner_id"] in RUNNERS, "unknown runner_id")
    _require(out["environment"] in ENVIRONMENTS, "unknown environment")
    _require(out["status"] in STATUSES, "unknown status")
    _require(isinstance(out["dry_run"], bool), "dry_run must be boolean")
    _require(not out["dry_run"] or out["environment"] == "test",
             "dry_run requires environment=test")
    started = _timestamp(out["started_at"], "started_at")
    finished = _timestamp(out["finished_at"], "finished_at")
    _require(finished >= started, "finished_at precedes started_at")
    _require(isinstance(out["product_ids"], list), "product_ids must be an array")
    _require(all(isinstance(x, str) and x for x in out["product_ids"]),
             "product_ids entries must be non-empty strings")
    _require(len(out["product_ids"]) == len(set(out["product_ids"])),
             "product_ids must be unique")
    _require(out["error"] is None or isinstance(out["error"], str),
             "error must be null or a string")
    if out["status"] == "failed":
        _require(bool(out["error"]), "failed event requires error")

    _require(isinstance(out["effects"], list), "effects must be an array")
    for index, effect in enumerate(out["effects"]):
        _require(isinstance(effect, dict), f"effects[{index}] must be an object")
        for field in ("provider", "action", "status", "receipt", "evidence",
                      "null_reason", "simulated"):
            _require(field in effect, f"effects[{index}].{field} is required")
        _require(all(isinstance(effect[field], str) and effect[field]
                     for field in ("provider", "action", "status")),
                 f"effects[{index}] identity fields must be non-empty strings")
        _require(isinstance(effect["simulated"], bool),
                 f"effects[{index}].simulated must be boolean")
        _require(bool(effect["receipt"]) or bool(effect["null_reason"]),
                 f"effects[{index}] requires receipt or null_reason")
        if effect["receipt"]:
            _require(bool(effect["evidence"]),
                     f"effects[{index}] receipt requires evidence")

    _require(isinstance(out["metrics"], list) and out["metrics"],
             "metrics must be a non-empty array")
    for index, metric in enumerate(out["metrics"]):
        _require(isinstance(metric, dict), f"metrics[{index}] must be an object")
        for field in ("name", "product_id", "value", "unit", "observed_at",
                      "source", "evidence", "null_reason", "simulated"):
            _require(field in metric, f"metrics[{index}].{field} is required")
        _require(isinstance(metric["name"], str) and metric["name"],
                 f"metrics[{index}].name must be non-empty")
        _require(metric["product_id"] is None or isinstance(metric["product_id"], str),
                 f"metrics[{index}].product_id must be null or string")
        _require(isinstance(metric["source"], str) and metric["source"],
                 f"metrics[{index}].source must be non-empty")
        _require(isinstance(metric["simulated"], bool),
                 f"metrics[{index}].simulated must be boolean")
        _timestamp(metric["observed_at"], f"metrics[{index}].observed_at")
        if metric["value"] is None:
            _require(bool(metric["null_reason"]),
                     f"metrics[{index}] null value requires null_reason")
        else:
            _require(isinstance(metric["value"], (int, float)) and
                     not isinstance(metric["value"], bool),
                     f"metrics[{index}].value must be numeric or null")
            _require(bool(metric["unit"]), f"metrics[{index}] value requires unit")
            _require(bool(metric["evidence"]),
                     f"metrics[{index}] value requires evidence")

    _require(isinstance(out["evidence"], list) and out["evidence"],
             "evidence must be a non-empty array")
    for index, item in enumerate(out["evidence"]):
        _require(isinstance(item, dict), f"evidence[{index}] must be an object")
        _require(isinstance(item.get("path"), str) and item["path"],
                 f"evidence[{index}].path is required")
        _require(isinstance(item.get("sha256"), str) and
                 SHA256_RE.fullmatch(item["sha256"]) is not None,
                 f"evidence[{index}].sha256 must be lowercase hex")
        _require(isinstance(item.get("bytes"), int) and item["bytes"] >= 0,
                 f"evidence[{index}].bytes must be non-negative")
        _require(isinstance(item.get("kind"), str) and item["kind"],
                 f"evidence[{index}].kind is required")

    if out["environment"] == "production":
        simulated = [x for x in out["effects"] + out["metrics"] if x["simulated"]]
        _require(not simulated, "production event cannot contain simulated data")
    return out


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class RunStore:
    def __init__(self, final_path: pathlib.Path, delivery_path: pathlib.Path):
        self.final_path = pathlib.Path(final_path)
        self.delivery_path = pathlib.Path(delivery_path)
        self.lock_path = self.final_path.with_suffix(self.final_path.suffix + ".lock")

    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def final_events(self) -> list[dict]:
        return _read_jsonl(self.final_path)

    def deliveries(self) -> list[dict]:
        return _read_jsonl(self.delivery_path)

    def record_final(self, event: dict) -> RecordResult:
        checked = validate_event(event)
        key = (checked["runner_id"], checked["run_id"])
        with self._locked():
            for existing in self.final_events():
                if (existing["runner_id"], existing["run_id"]) == key:
                    if _canonical(existing) != _canonical(checked):
                        raise ConflictError(f"conflicting replay for {key[0]}:{key[1]}")
                    return RecordResult(existing, False)
            self.final_path.parent.mkdir(parents=True, exist_ok=True)
            with self.final_path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical(checked) + "\n")
                handle.flush()
            return RecordResult(checked, True)

    def delivery_for(self, runner_id: str, run_id: str) -> dict | None:
        for row in self.deliveries():
            if row.get("runner_id") == runner_id and row.get("run_id") == run_id:
                return row
        return None

    def record_delivery(self, runner_id: str, run_id: str, receipt: dict) -> dict:
        with self._locked():
            existing = self.delivery_for(runner_id, run_id)
            if existing:
                return existing
            row = {
                "schema_version": "marketing.delivery.v1",
                "runner_id": runner_id,
                "run_id": run_id,
                "delivered_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
                "status": receipt.get("status"),
                "chat_id": receipt.get("chat_id"),
                "message_ids": receipt.get("message_ids") or [],
            }
            self.delivery_path.parent.mkdir(parents=True, exist_ok=True)
            with self.delivery_path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical(row) + "\n")
                handle.flush()
            return row


def evidence_item(path: pathlib.Path, kind: str) -> dict:
    path = pathlib.Path(path)
    payload = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "kind": kind,
    }


def render_telegram(event: dict) -> str:
    event = validate_event(event)
    lines = [
        f"{event['runner_id']} · {event['status']} · {event['environment']}",
        f"run {event['run_id']}",
    ]
    for effect in event["effects"]:
        outcome = effect["receipt"] or f"null:{effect['null_reason']}"
        lines.append(f"effect {effect['provider']}/{effect['action']}: {outcome}")
    for metric in event["metrics"]:
        value = (f"{metric['value']} {metric['unit']}" if metric["value"] is not None
                 else f"null:{metric['null_reason']}")
        product = f"[{metric['product_id']}] " if metric["product_id"] else ""
        lines.append(f"{product}{metric['name']}={value} ({metric['source']})")
    first = event["evidence"][0]
    lines.append(f"evidence {first['path']} sha256:{first['sha256'][:12]}")
    if event["error"]:
        lines.append(f"error {event['error']}")
    return "\n".join(lines)


def record_and_deliver(event: dict, store: RunStore,
                       send_text: Callable[[str], dict]) -> dict:
    recorded = store.record_final(event).event
    existing = store.delivery_for(recorded["runner_id"], recorded["run_id"])
    if existing:
        return existing
    receipt = send_text(render_telegram(recorded))
    return store.record_delivery(recorded["runner_id"], recorded["run_id"], receipt)
