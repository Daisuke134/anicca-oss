from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .mercor_human_gate import HumanGateStore
from .telegram import send_once


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_rows(value: Any, fields: tuple[str, ...]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        row = {
            field: item[field].strip()
            for field in fields
            if isinstance(item.get(field), str) and item[field].strip()
        }
        if row:
            rows.append(row)
    return rows


def delivery_state(delivery: Mapping[str, Any]) -> str:
    return (
        "ack"
        if delivery.get("status") == "sent" and delivery.get("message_id")
        else "delivery_unknown"
    )


def terminal_result(*, result_path: Path, reason: str) -> dict[str, Any]:
    """Keep the wake receipt useful without copying private browser artifacts."""
    try:
        source = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        source = None
    if not isinstance(source, Mapping) or not isinstance(source.get("status"), str):
        return {
            "status": "blocked" if reason == "mercor_pass_already_running" else "failed",
            "inspected_listings": [],
            "submitted": [],
            "needs_human": [],
            "blocked": [reason],
            "evidence": {"page_url": "", "screenshot_path": "", "dom_path": ""},
        }
    blocked = [
        item.strip()
        for item in source.get("blocked", [])
        if isinstance(item, str) and item.strip()
    ]
    if reason != "success" and reason not in blocked:
        blocked.append(reason)
    return {
        "status": (
            "blocked"
            if reason == "mercor_pass_already_running"
            else "failed"
            if reason != "success"
            else source["status"].strip() or "failed"
        ),
        "inspected_listings": _safe_rows(
            source.get("inspected_listings"),
            ("listing_id", "title", "url", "decision", "ranking_band")
        ),
        "submitted": _safe_rows(
            source.get("submitted"), ("listing_id", "title", "url", "status")
        ),
        "needs_human": [
            item.strip()
            for item in source.get("needs_human", [])
            if isinstance(item, str) and item.strip()
        ],
        "blocked": blocked,
        "evidence": {"page_url": "", "screenshot_path": "", "dom_path": ""},
    }


def _labels(rows: Any, *, field: str = "title") -> list[str]:
    if not isinstance(rows, list):
        return []
    values: list[str] = []
    for row in rows:
        if isinstance(row, Mapping):
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    return values


def build_pass_message(
    *, run_id: str, result: Mapping[str, Any], human_gate_ids: list[str] | None = None
) -> str:
    status = str(result.get("status") or "unknown")
    inspected = []
    for row in result.get("inspected_listings", []):
        if not isinstance(row, Mapping):
            continue
        title = row.get("title")
        decision = row.get("decision")
        if isinstance(title, str) and title.strip():
            label = title.strip()
            if isinstance(decision, str) and decision.strip():
                label += f" [{decision.strip()}]"
            inspected.append(label)
    submitted = _labels(result.get("submitted"))
    needs_human = _labels(result.get("needs_human"), field="reason") or [
        value.strip()
        for value in result.get("needs_human", [])
        if isinstance(value, str) and value.strip()
    ]
    blocked = _labels(result.get("blocked"), field="reason") or [
        value.strip()
        for value in result.get("blocked", [])
        if isinstance(value, str) and value.strip()
    ]
    fields = [f"Codex::: Mercor pass {run_id} status={status}"]
    if inspected:
        fields.append("inspected=" + "; ".join(inspected[:5]))
    if submitted:
        fields.append("submitted=" + ", ".join(submitted[:3]))
    if human_gate_ids:
        fields.append("human_gate_ids=" + ", ".join(human_gate_ids[:3]))
    elif needs_human:
        fields.append("needs_human=" + ", ".join(needs_human[:3]))
    if blocked:
        fields.append("blocked=" + ", ".join(blocked[:3]))
    if not any((inspected, submitted, needs_human, blocked)):
        fields.append("detail=no grounded action")
    return "\n".join(fields)


def report_pass(*, run_id: str, result_path: Path, outbox: Path, gate_store: Path | None = None) -> dict[str, Any]:
    result = json.loads(Path(result_path).read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Mercor pass result must be an object")
    gate_ids: list[str] = []
    if gate_store is not None:
        evidence = result.get("evidence") if isinstance(result.get("evidence"), Mapping) else {}
        evidence_ref = str(evidence.get("page_url") or f"run:{run_id}")
        store = HumanGateStore(gate_store)
        if (
            result.get("status") not in {"blocked", "failed"}
            and isinstance(result.get("inspected_listings"), list)
            and result["inspected_listings"]
        ):
            for identity_key in ("mercor_authentication", "resume_artifact"):
                store.resolve(
                    identity_key=identity_key,
                    run_id=run_id,
                    evidence_ref=f"run:{run_id}",
                )
        for reason in result.get("needs_human", []):
            if isinstance(reason, str) and reason.strip():
                gate_id = store.record(
                    run_id=run_id,
                    reason=reason,
                    evidence_ref=evidence_ref,
                )["gate_id"]
                if gate_id not in gate_ids:
                    gate_ids.append(gate_id)
    message = build_pass_message(
        run_id=run_id,
        result=result,
        human_gate_ids=gate_ids,
    )
    event_key = f"mercor-pass:{run_id}"
    try:
        delivery = send_once(database=outbox, event_key=event_key, message=message)
        status = {
            **delivery,
            "event_key": event_key,
            "delivery": delivery_state(delivery),
            "message": message,
            "human_gate_ids": gate_ids,
        }
    except Exception as error:  # reporting must not suppress the pass result
        status = {
            "event_key": event_key,
            "delivery": "delivery_unknown",
            "reason": type(error).__name__,
            "message": message,
            "human_gate_ids": gate_ids,
        }
    return status


def terminal_report(
    *,
    run_id: str,
    result_path: Path,
    reason: str,
    outbox: Path,
    gate_store: Path | None,
    terminal_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    result = terminal_result(result_path=result_path, reason=reason)
    _write_private_json(terminal_path, result)
    receipt = report_pass(
        run_id=run_id,
        result_path=terminal_path,
        outbox=outbox,
        gate_store=gate_store,
    )
    _write_private_json(output_path, receipt)
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--terminal-report", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--reason", default="mercor_result_missing")
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--gate-store", type=Path)
    parser.add_argument("--terminal", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.terminal_report:
        if args.terminal is None:
            parser.error("terminal-report requires --terminal")
        result = terminal_report(
            run_id=args.run_id,
            result_path=args.result,
            reason=args.reason,
            outbox=args.outbox,
            gate_store=args.gate_store,
            terminal_path=args.terminal,
            output_path=args.output,
        )
    else:
        result = report_pass(
            run_id=args.run_id,
            result_path=args.result,
            outbox=args.outbox,
            gate_store=args.gate_store,
        )
        _write_private_json(args.output, result)
    print(json.dumps({"delivery": result["delivery"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
