#!/usr/bin/env python3
"""ceo_status.py — REQ-CEO-005/007/011/013 backend for bin/ceo-status.sh: pure read-and-render (the
one effectful step -- the budget enforcement check -- is delegated to a bin/budget-check.sh
subprocess call, the same script REQ-CEO-009's dispatch gate uses, so ceo-status.sh's own code
stays read+render). Always exits 0 (a status tool must never fail the shell script that composes
bin/status.sh, REQ-CEO-007).
"""
import json
import os
import subprocess
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "lib"))
from ceo_allocation import effective_registry  # noqa: E402

LEDGER_NAMES = ["cost-events", "loop-evaluations", "ceo-decisions", "lessons"]


def _ensure_ledgers(ledgers_dir):
    os.makedirs(ledgers_dir, exist_ok=True)
    for name in LEDGER_NAMES:
        path = os.path.join(ledgers_dir, f"{name}.jsonl")
        if not os.path.exists(path):
            open(path, "a").close()


def _read_jsonl_tolerant(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _print_compute_runway(ledgers_dir):
    rows = _read_jsonl_tolerant(os.path.join(ledgers_dir, "compute-runway.jsonl"))
    if not rows:
        print("compute_runway: unknown")
        return
    latest = rows[-1]
    print(f"compute_runway: status={latest.get('status')} reset_at={latest.get('reset_at')} "
          f"provider={latest.get('provider')} affected_loops={latest.get('affected_loops')}")


def main():
    base = sys.argv[1]
    ledgers_dir = os.path.join(base, "ledgers")
    _ensure_ledgers(ledgers_dir)

    try:
        registry = effective_registry(os.environ.get("CEO_CONFIG_ROOT", _REPO_ROOT), base)
    except Exception as e:
        print(f"registry: corrupt ({e})")
        _print_compute_runway(ledgers_dir)
        return 0

    print("registry: ok")
    _print_compute_runway(ledgers_dir)

    cost_totals = {}
    for row in _read_jsonl_tolerant(os.path.join(ledgers_dir, "cost-events.jsonl")):
        loop = row.get("loop")
        usd = row.get("usd_estimate")
        if loop and isinstance(usd, (int, float)) and not isinstance(usd, bool):
            cost_totals[loop] = cost_totals.get(loop, 0.0) + usd

    last_eval = {}
    for row in _read_jsonl_tolerant(os.path.join(ledgers_dir, "loop-evaluations.jsonl")):
        loop = row.get("loop")
        if loop:
            last_eval[loop] = row  # append-only + in-order iteration -> last write wins

    for loop, entry in registry.get("loops", {}).items():
        status = entry.get("status", "?")
        alloc = entry.get("allocation", {}) or {}
        evidence = entry.get("evidence_path", "?")
        cost = cost_totals.get(loop, 0.0)
        revenue = "unknown"
        if loop in last_eval:
            row = last_eval[loop]
            revenue = row.get("mrr_usd", row.get("revenue_usd", "unknown"))
        print(f"loop={loop} status={status} allocation={alloc.get('status')} "
              f"mult={alloc.get('pass_frequency_multiplier')} cadence={entry.get('cadence_contract')} "
              f"evidence={evidence} cost={cost} revenue={revenue}")

    # per-loop budget lines -- delegates the one effectful check to bin/budget-check.sh (no --loop:
    # every registry loop in one call), keeping this script's own code read+render only.
    budget_check_sh = os.path.join(_REPO_ROOT, "bin", "budget-check.sh")
    if os.path.isfile(budget_check_sh):
        env = dict(os.environ)
        env["CEO_STATE_DIR"] = base
        try:
            proc = subprocess.run(["bash", budget_check_sh], env=env, capture_output=True,
                                   text=True, timeout=30)
            if proc.stdout:
                sys.stdout.write(proc.stdout)
        except Exception as e:
            print(f"budget: ERROR ({e})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
