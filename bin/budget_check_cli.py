#!/usr/bin/env python3
"""budget_check_cli.py — REQ-CEO-013 backend for bin/budget-check.sh. Prints one `budget: <loop>
...` line per loop checked (all registry loops, or exactly one via --loop). Always exits 0 --
this tool is informational; the pause side-effect itself (registry.allocation.status="paused")
is what a subsequent registry_enforce_or_exit call detects (REQ-CEO-009 step 0).
"""
import json
import os
import sys

_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
sys.path.insert(0, _LIB_DIR)
from ceo_budget import check_loop, CANONICAL_LOOPS  # noqa: E402


def _registry_loop_keys(base):
    config_root = os.environ.get("CEO_CONFIG_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from ceo_allocation import effective_registry
        registry = effective_registry(config_root, base)
        loops = registry.get("loops")
        if isinstance(loops, dict) and loops:
            return list(loops.keys())
    except Exception:
        pass
    return None


def main():
    base = sys.argv[1]
    loop_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

    if loop_arg:
        targets = [loop_arg]
    else:
        targets = _registry_loop_keys(base) or list(CANONICAL_LOOPS)

    for loop in targets:
        try:
            result = check_loop(base, loop)
        except Exception as e:
            print(f"budget: {loop} ERROR ({e})")
            continue
        if not result.get("configured"):
            print(f"budget: {loop} not configured")
            continue
        status = result.get("breach") or "ok"
        enforcement = result.get("enforcement") or "active"
        print(
            f"budget: {loop} monthly={result['monthly_sum']}/{result['monthly_soft']}/{result['monthly_hard']} "
            f"weekly={result['weekly_sum']}/{result['weekly_soft']}/{result['weekly_hard']} "
            f"status={status} enforcement={enforcement}"
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
