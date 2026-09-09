#!/usr/bin/env python3
"""record_cost_event.py — REQ-CEO-006 backend for bin/record-cost-event.sh. Appends ONE JSONL row
{ts, loop, usd_estimate} to ledgers/cost-events.jsonl. An unknown loop name (not a key in
config/loop-registry.json, or the registry is itself absent/unreadable) is still appended, flagged
`known_loop: false` -- never silently dropped, never crashes (NFR-002).
"""
import json
import os
import sys
import time

# REQ-CEO-001's 9 canonical loop keys -- used ONLY as a bootstrap fallback when
# config/loop-registry.json itself is absent/unreadable (a fixed spec constant, not a judgment).
CANONICAL_LOOPS = {
    "bounty", "affiliate", "gig", "life-manager", "explorer",
    "capafy", "article", "pm", "hl",
}


def _known_loops(base):
    config_root = os.environ.get("CEO_CONFIG_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        sys.path.insert(0, os.path.join(config_root, "lib"))
        from ceo_allocation import effective_registry
        registry = effective_registry(config_root, base)
        loops = registry.get("loops")
        if isinstance(loops, dict) and loops:
            return set(loops.keys())
    except Exception:
        pass
    return set(CANONICAL_LOOPS)


def main():
    base, loop, usd_arg = sys.argv[1], sys.argv[2], sys.argv[3]
    usd_estimate = float(usd_arg)

    ledgers_dir = os.path.join(base, "ledgers")
    os.makedirs(ledgers_dir, exist_ok=True)

    row = {"ts": int(time.time()), "loop": loop, "usd_estimate": usd_estimate}
    if loop not in _known_loops(base):
        row["known_loop"] = False

    with open(os.path.join(ledgers_dir, "cost-events.jsonl"), "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
