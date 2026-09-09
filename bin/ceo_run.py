#!/usr/bin/env python3
"""ceo_run.py — REQ-CEO-008/REQ-CEO-015 backend for bin/ceo-run.sh's deterministic modes:

    ceo_run.py apply-decision <base> <payload.json>
        Validates an agent-proposed allocation decision (enum/range/unknown-loop/negative-capital
        rejection) and, only if valid, atomically writes the runtime allocation override AND
        appends one row to ledgers/ceo-decisions.jsonl (registry write first, per REQ-CEO-008's
        documented order -- a write failure leaves ceo-decisions.jsonl untouched).

    ceo_run.py light-pass <base>
        Runs bin/budget-check.sh for every registry loop (deterministic, no agent judgment, no
        tmux/claude spawn -- that stays in bin/ceo-run.sh's bash default-mode branch).

    ceo_run.py record-pass <base> <summary.json>
        REQ-CEO-021: persists the weekly full-evaluation pass's own outcome. Validates the
        summary JSON is an object (rejects missing file / invalid JSON / non-object, e.g. an
        array) and, only if valid, appends ONE row to ledgers/ceo-decisions.jsonl shaped
        {ts, type:"weekly-eval", reviewed, changed, reason}. Called by bin/ceo-run.sh's no-arg
        STARTUP prompt AFTER the CEO core judges, whether or not it changed anything that pass.
"""
import json
import os
import subprocess
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIB_DIR = os.path.join(_REPO_ROOT, "lib")
sys.path.insert(0, _LIB_DIR)
from registry_write_gate import append_jsonl  # noqa: E402
from ceo_budget import CANONICAL_LOOPS  # noqa: E402
from ceo_unit_economics import validate_allocation_policy  # noqa: E402
from ceo_allocation import effective_registry, write_allocation  # noqa: E402

ALLOCATION_STATUS_ENUM = {"normal", "paused", "reduce", "double_down"}


def _load_json(path):
    with open(path) as f:
        return json.load(f)


def validate_decision(payload, registry):
    """Returns a list of rejection reasons (empty = valid). REQ-CEO-003/REQ-CEO-008."""
    errs = []
    if not isinstance(payload, dict):
        return ["payload is not a JSON object"]

    loop = payload.get("loop")
    if loop not in registry.get("loops", {}):
        errs.append(f"unknown loop {loop!r}")
        return errs

    alloc = payload.get("allocation")
    if not isinstance(alloc, dict):
        return errs + ["allocation missing or not an object"]

    status = alloc.get("status")
    if status not in ALLOCATION_STATUS_ENUM:
        errs.append(f"invalid allocation.status {status!r}")

    mult = alloc.get("pass_frequency_multiplier")
    if not isinstance(mult, (int, float)) or isinstance(mult, bool):
        errs.append(f"pass_frequency_multiplier {mult!r} is not numeric")
    elif not (0.1 <= mult <= 10.0):
        errs.append(f"pass_frequency_multiplier {mult!r} out of [0.1, 10.0]")

    cap = alloc.get("capital_cap_usd", "MISSING")
    if cap == "MISSING":
        errs.append("capital_cap_usd missing")
    elif cap is not None and (not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap < 0):
        errs.append(f"capital_cap_usd {cap!r} must be >=0 or null")

    return errs


def cmd_apply_decision(base, payload_path):
    decisions_path = os.path.join(base, "ledgers", "ceo-decisions.jsonl")
    config_root = os.environ.get("CEO_CONFIG_ROOT", _REPO_ROOT)

    try:
        payload = _load_json(payload_path)
    except Exception as e:
        print(f"ceo-run: invalid payload JSON ({e})", file=sys.stderr)
        return 1

    try:
        registry = effective_registry(config_root, base)
    except Exception as e:
        print(f"ceo-run: cannot read repository registry ({e})", file=sys.stderr)
        return 1

    errs = validate_decision(payload, registry)
    policy_config_path = os.path.join(config_root, "config", "ceo-unit-economics.json")
    if os.path.isfile(policy_config_path) and not errs:
        snapshot_path = os.path.join(base, "ledgers", "ceo-unit-economics.latest.json")
        try:
            snapshot = _load_json(snapshot_path)
            errs.extend(validate_allocation_policy(snapshot, payload["loop"], payload["allocation"]["status"]))
        except Exception as e:
            errs.append(f"unit_economics_snapshot_unavailable ({e})")
    if errs:
        print("ceo-run: decision REJECTED: " + "; ".join(errs), file=sys.stderr)
        return 1

    loop = payload["loop"]
    try:
        write_allocation(base, loop, payload["allocation"])
    except Exception as e:
        print(f"ceo-run: registry write failed ({e}); decision NOT applied, ceo-decisions.jsonl untouched", file=sys.stderr)
        return 1

    append_jsonl(decisions_path, {
        "ts": int(time.time()), "type": "allocation-change", "loop": loop, "reason": "ceo_decision",
        "allocation": payload["allocation"],
    })
    print(f"ceo-run: applied decision for loop '{loop}'")
    return 0


def cmd_record_pass(base, summary_path):
    """REQ-CEO-021: persists a weekly full-evaluation pass's outcome. Rejects (non-zero exit,
    zero mutation) if summary_path is missing, not valid JSON, or not a JSON object (e.g. an
    array). reviewed/changed default to [] and reason defaults to "" when absent -- those are
    informational fields, not structural gates (the agent's own judgment about what it reviewed/
    changed/why is never validated for "correctness" here, only its shape)."""
    decisions_path = os.path.join(base, "ledgers", "ceo-decisions.jsonl")

    try:
        summary = _load_json(summary_path)
    except Exception as e:
        print(f"ceo-run: invalid summary JSON ({e})", file=sys.stderr)
        return 1

    if not isinstance(summary, dict):
        print(f"ceo-run: record-pass REJECTED: summary must be a JSON object, got {type(summary).__name__}",
              file=sys.stderr)
        return 1

    append_jsonl(decisions_path, {
        "ts": int(time.time()),
        "type": "weekly-eval",
        "reviewed": summary.get("reviewed") or [],
        "changed": summary.get("changed") or [],
        "reason": summary.get("reason") or "",
    })
    print("ceo-run: recorded weekly-eval pass outcome")
    return 0


def cmd_apply_evaluation(base, evaluation_path):
    """Validate an entire compact weekly response before applying any decision."""
    try:
        evaluation = _load_json(evaluation_path)
        snapshot = _load_json(os.path.join(base, "ledgers", "ceo-unit-economics.latest.json"))
        registry = effective_registry(os.environ.get("CEO_CONFIG_ROOT", _REPO_ROOT), base)
    except Exception as e:
        print(f"ceo-run: evaluation unavailable ({e})", file=sys.stderr)
        return 1
    if not isinstance(evaluation, dict) or evaluation.get("status") != "ok":
        print("ceo-run: evaluation REJECTED: invalid root/status", file=sys.stderr)
        return 1
    decisions = evaluation.get("decisions")
    if not isinstance(decisions, list):
        print("ceo-run: evaluation REJECTED: decisions must be an array", file=sys.stderr)
        return 1

    errors = []
    seen = set()
    for index, decision in enumerate(decisions):
        decision_errors = validate_decision(decision, registry)
        if isinstance(decision, dict) and not decision_errors:
            loop = decision["loop"]
            if loop in seen:
                decision_errors.append("duplicate loop decision")
            seen.add(loop)
            decision_errors.extend(validate_allocation_policy(
                snapshot, loop, decision["allocation"]["status"]
            ))
        errors.extend(f"decision[{index}]: {error}" for error in decision_errors)
    if errors:
        print("ceo-run: evaluation REJECTED: " + "; ".join(errors), file=sys.stderr)
        return 1

    changed = []
    for index, decision in enumerate(decisions):
        payload_path = os.path.join(base, "ledgers", f".ceo-evaluation-decision-{os.getpid()}-{index}.json")
        try:
            with open(payload_path, "w", encoding="utf-8") as handle:
                json.dump(decision, handle)
            if cmd_apply_decision(base, payload_path) != 0:
                return 1
            changed.append(decision["loop"])
        finally:
            try:
                os.unlink(payload_path)
            except OSError:
                pass

    append_jsonl(os.path.join(base, "ledgers", "ceo-decisions.jsonl"), {
        "ts": int(time.time()), "type": "weekly-eval",
        "reviewed": sorted(snapshot.get("loops", {}).keys()),
        "changed": changed,
        "reason": str(evaluation.get("reason") or "")[:500],
    })
    print(f"ceo-run: weekly evaluation applied {len(changed)} decision(s)")
    return 0


def cmd_light_pass(base):
    try:
        registry = effective_registry(os.environ.get("CEO_CONFIG_ROOT", _REPO_ROOT), base)
        loops = list(registry.get("loops", {}).keys())
    except Exception:
        loops = list(CANONICAL_LOOPS)

    budget_check_sh = os.path.join(_REPO_ROOT, "bin", "budget-check.sh")
    env = dict(os.environ)
    env["CEO_STATE_DIR"] = base

    snapshot_cli = os.path.join(_REPO_ROOT, "bin", "ceo_unit_economics.py")
    snapshot_proc = subprocess.run(
        [sys.executable, snapshot_cli, base, "--config-root", env.get("CEO_CONFIG_ROOT", _REPO_ROOT)],
        capture_output=True,
        text=True,
    )
    if snapshot_proc.stdout:
        sys.stdout.write(snapshot_proc.stdout)
    if snapshot_proc.stderr:
        sys.stderr.write(snapshot_proc.stderr)

    for loop in loops:
        try:
            proc = subprocess.run(["bash", budget_check_sh, "--loop", loop], env=env,
                                   capture_output=True, text=True, timeout=30)
            if proc.stdout:
                sys.stdout.write(proc.stdout)
            if proc.stderr:
                sys.stderr.write(proc.stderr)
        except Exception as e:
            print(f"budget: {loop} ERROR ({e})")

    return 0


def main():
    mode = sys.argv[1]
    base = sys.argv[2]
    if mode == "apply-decision":
        rc = cmd_apply_decision(base, sys.argv[3])
    elif mode == "light-pass":
        rc = cmd_light_pass(base)
    elif mode == "record-pass":
        rc = cmd_record_pass(base, sys.argv[3])
    elif mode == "apply-evaluation":
        rc = cmd_apply_evaluation(base, sys.argv[3])
    else:
        print(f"ceo-run: unknown mode {mode!r}", file=sys.stderr)
        rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
