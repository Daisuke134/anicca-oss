import json
from pathlib import Path


def write_repair_run_gates(gates: Path, note_error: str) -> None:
    """Create the minimal immutable receipts for one failed Note destination."""
    run_id = gates.parent.name
    gates.mkdir(parents=True, exist_ok=True)
    values = {
        "generation-state.json": {
            "version": 1,
            "run_id": run_id,
            "status": "provider-returned",
        },
        "quality-self-heal.json": {
            "version": 2,
            "run_id": run_id,
            "action": "ready_to_freeze",
        },
        "publication-state.json": {
            "version": 1,
            "run_id": run_id,
            "pairs": {
                "note/ja": {
                    "platform": "note",
                    "lang": "ja",
                    "status": "failed",
                    "error": note_error,
                }
            },
        },
        "resume-failure-circuit.json": {
            "version": 1,
            "pairs": {"note/ja": {"open": True, "signature": note_error}},
        },
    }
    for name, value in values.items():
        (gates / name).write_text(json.dumps(value), encoding="utf-8")
