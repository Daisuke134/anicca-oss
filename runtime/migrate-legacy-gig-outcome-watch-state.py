#!/usr/bin/env python3
"""Mirror the outcome watcher's three business files into its Life Manager root."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from legacy_state_mirror import mirror_files


FILES = {
    Path("history.jsonl"): Path("history.jsonl"),
    Path("domain-skills.jsonl"): Path("domain-skills.jsonl"),
    Path("last-sent"): Path("last-sent"),
}


def migrate(source: Path, target: Path, *, seal: bool = False) -> dict:
    if source.is_symlink():
        raise ValueError(f"symlink source root is not allowed: {source}")
    if not source.exists():
        return {"copied": 0, "skipped": 0, "verified": 0, "sealed": bool(seal)}
    present = {}
    for relative in FILES:
        path = source / relative
        if path.is_symlink():
            raise ValueError(f"symlink source is not allowed: {path}")
        if path.exists():
            if not path.is_file():
                raise ValueError(f"source is not a regular file: {path}")
            present[relative] = FILES[relative]
    return mirror_files(
        source,
        target,
        present,
        marker_name="legacy-business-state-migration.json",
        seal=seal,
        allow_existing_target=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.home() / ".local/state/anicca/gig-outcome-watch",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path.home() / ".local/state/life-manager/gig-outcome-watch",
    )
    parser.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(args.source, args.target, seal=args.seal)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"gig outcome watch migration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
