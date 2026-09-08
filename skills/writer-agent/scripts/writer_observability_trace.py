#!/usr/bin/env python3
"""Build one local trace from immutable Writer receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publication_contract  # noqa: E402
from artifact_attribution import AttributionKeyError, artifact_id  # noqa: E402
from writer_observability_event import EventError, wrap_receipt  # noqa: E402


# A pair in one of these states has no open publisher failure to repair: it is
# either already shipped or deliberately dormant.  A stale circuit row must
# never resurrect it as an incident.
SETTLED_DESTINATION_STATUSES = frozenset({"live", "skipped"})


class TraceError(ValueError):
    pass


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise TraceError(f"invalid receipt: {path.name}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TraceError(f"receipt is not an object: {path.name}")
    return value


def _destination_context(run_id: str, pair: str) -> dict[str, Any]:
    platform, _, lang = pair.rpartition("/")
    try:
        key = artifact_id(run_id=run_id, platform=platform, lang=lang)
    except AttributionKeyError:
        key = None
    role = publication_contract.revenue_role(pair)
    return {
        "pair": pair,
        "artifact_id": key,
        "revenue_role": role,
        "blocking": role == publication_contract.REVENUE_SET,
    }


def build_trace(run_dir: Path, observed_at: str) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    gates = run_dir / "gates"
    paths: dict[str, Path] = {
        "generation": gates / "generation-state.json",
        "quality": gates / "quality-self-heal.json",
        "publication": gates / "publication-state.json",
    }
    generation = _load(paths["generation"])
    quality = _load(paths["quality"])
    publication = _load(paths["publication"])
    if generation is None or quality is None:
        raise TraceError("generation and quality receipts are required")

    events: dict[str, dict[str, Any]] = {
        phase: wrap_receipt(run_dir=run_dir, receipt_path=path, phase=phase,
                            observed_at=observed_at)
        for phase, path in paths.items() if path.exists()
    }
    invalid_receipts: set[str] = set()
    trace_id = secrets.token_hex(16)
    records: list[dict[str, Any]] = []

    lifecycle_names = {
        "research": ("research-state.json", "research-tool-failures.json"),
        "readback": ("readback-state.json",),
        "metrics": ("metrics-state.json",),
        "money": ("money-state.json",),
        "learning": ("learning-state.json",),
        "reporting": ("reporting-state.json",),
    }
    lifecycle_paths: dict[str, Path | None] = {}
    for phase, names in lifecycle_names.items():
        candidates = [gates / name for name in names]
        if phase == "readback":
            candidates.extend(sorted(gates.glob("*readback*.json")))
            candidates.extend(sorted(gates.glob("render-verify-*.json")))
        lifecycle_paths[phase] = next((path for path in candidates if path.is_file()), None)
    reserved = {path.name for path in paths.values()}
    reserved.update(name for names in lifecycle_names.values() for name in names)
    gate_paths = [
        path for path in sorted(gates.glob("*.json"))
        if path.name not in reserved
        and "readback" not in path.name
        and not path.name.startswith("render-verify-")
    ]
    phase_source_paths: dict[str, Path] = {
        phase: path for phase, path in paths.items() if path.is_file()
    }
    for phase, path in lifecycle_paths.items():
        if path is not None:
            phase_source_paths[phase] = path
            try:
                events[phase] = wrap_receipt(run_dir=run_dir, receipt_path=path, phase=phase,
                                             observed_at=observed_at)
            except (EventError, json.JSONDecodeError):
                invalid_receipts.add(phase)
    for path in gate_paths:
        phase = f"gate:{path.stem}"
        phase_source_paths[phase] = path
        try:
            events[phase] = wrap_receipt(run_dir=run_dir, receipt_path=path, phase=phase,
                                         observed_at=observed_at)
        except (EventError, json.JSONDecodeError):
            invalid_receipts.add(phase)

    destination_pairs = (
        publication.get("pairs", {})
        if isinstance(publication, dict) and isinstance(publication.get("pairs"), dict)
        else {}
    )
    # The deterministic retry owner records its own terminal verdict here.  An
    # open row means retry is blocked and the pair is genuinely failed even
    # while `publication-state.json` still reads `intent`.
    circuit = _load(gates / "resume-failure-circuit.json")
    circuit_pairs = (
        circuit.get("pairs", {})
        if isinstance(circuit, dict) and isinstance(circuit.get("pairs"), dict)
        else {}
    )
    for pair in destination_pairs:
        phase = f"destination:{pair}"
        if "publication" in events:
            events[phase] = events["publication"]
        if "publication" in phase_source_paths:
            phase_source_paths[phase] = phase_source_paths["publication"]
    phases = ["research", "generation"]
    phases.extend(f"gate:{path.stem}" for path in gate_paths)
    phases.extend(("quality", "publication"))
    phases.extend(f"destination:{pair}" for pair in sorted(destination_pairs))
    phases.extend(("readback", "metrics", "money",
                   "learning", "reporting"))
    downstream = {"readback", "metrics", "money", "learning", "reporting"}
    for phase in phases:
        state = "observed"
        reason = None
        destination: dict[str, Any] | None = None
        outcome: Any = events.get(phase, {}).get("outcome")
        if phase in invalid_receipts:
            state, reason = "error", "invalid_receipt"
        elif phase == "research" and phase not in events:
            state, reason = "error", "expected_receipt_missing"
        if phase == "publication":
                if quality.get("action") == "block_freeze":
                    state, reason = "not_expected", "quality_block_freeze"
                elif publication is None:
                    state, reason = "error", "expected_receipt_missing"
                else:
                    pairs = publication.get("pairs")
                    outcome = {
                        key: value.get("status")
                        for key, value in pairs.items()
                        if isinstance(value, dict)
                    } if isinstance(pairs, dict) else {}
        elif phase.startswith("destination:"):
                pair = phase.removeprefix("destination:")
                entry = destination_pairs.get(pair, {})
                status = entry.get("status") if isinstance(entry, dict) else None
                outcome = {"field": "status", "value": status}
                destination = _destination_context(run_dir.name, pair)
                circuit_row = circuit_pairs.get(pair)
                circuit_open = (
                    isinstance(circuit_row, dict)
                    and circuit_row.get("open") is True
                    and status not in SETTLED_DESTINATION_STATUSES
                )
                if status == "skipped":
                    state, reason = "not_expected", "destination_dormant"
                elif status in {"unavailable", "failed", "error"}:
                    state = "error"
                    reason = (
                        entry.get("error")
                        or entry.get("last_error")
                        or f"destination_{status}"
                    )
                elif circuit_open:
                    # An unshipped intent whose retry circuit is open is a
                    # durable failure, not work still in flight.
                    state = "error"
                    reason = (
                        str(circuit_row.get("signature") or "").strip()
                        or f"destination_{status}_retry_circuit_open"
                    )
                if state == "error":
                    destination = {**destination, "error_signature": reason}
        elif phase in downstream and phase not in invalid_receipts:
            if quality.get("action") == "block_freeze":
                state, reason = "not_expected", "quality_block_freeze"
            elif phase not in events:
                state, reason = "error", "expected_receipt_missing"
        records.append({"phase": phase, "trace_id": trace_id,
                        "span_id": secrets.token_hex(8),
                        "state": state, "reason": reason,
                        "outcome": outcome,
                        "destination": destination,
                        "source_event_id": events.get(phase, {}).get("event_id"),
                        "source_receipt": (
                            events.get(phase, {}).get("source_receipt")
                            or (
                                {
                                    "path": str(phase_source_paths[phase].relative_to(run_dir)),
                                    "sha256": hashlib.sha256(
                                        phase_source_paths[phase].read_bytes()
                                    ).hexdigest(),
                                }
                                if phase in phase_source_paths
                                else None
                            )
                        ),
                        "release_commit": events.get(phase, {}).get("release_commit")})
    spans = records
    health = ("error" if any(s["state"] == "error" for s in spans)
              else "blocked_by_quality" if quality.get("action") == "block_freeze"
              else "observed")
    return {"schema": "writer.observability.trace", "version": 1,
            "run_id": run_dir.name, "observed_at": observed_at,
            "health": health, "spans": spans}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--run-dir", required=True, type=Path)
    build.add_argument("--observed-at", required=True)
    args = parser.parse_args()
    try:
        result = build_trace(args.run_dir, args.observed_at)
    except (TraceError, EventError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
