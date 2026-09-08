#!/usr/bin/env python3
"""Safely mirror legacy X social state before the Life Manager cutover."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from legacy_state_mirror import mirror_files, reject_symlinks, resolved_non_root, within


MAPPINGS = {
    "x-repost-en": "x-repost/en",
    "x-repost-ja": "x-repost/ja",
    "x-tweeter": "x-tweeter",
}
MARKER = ".legacy-migration.json"


def migrate(source_root: Path, target_root: Path, *, seal: bool = False) -> dict:
    source_root = resolved_non_root(source_root, "source root")
    target_root = resolved_non_root(target_root, "target root")
    if within(source_root, target_root) or within(target_root, source_root):
        raise ValueError("source and target overlap")
    files = {}
    for source_name, target_name in MAPPINGS.items():
        source = source_root / source_name
        if source.is_symlink():
            raise ValueError(f"symlink legacy source is not allowed: {source}")
        if not source.exists():
            continue
        if not source.is_dir():
            raise ValueError(f"legacy source is not a directory: {source}")
        reject_symlinks(source)
        for item in sorted(source.rglob("*")):
            if item.is_file():
                files[item.relative_to(source_root)] = Path(target_name) / item.relative_to(source)
    return mirror_files(source_root, target_root, files, marker_name=MARKER, seal=seal)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.home() / "loops")
    parser.add_argument(
        "--target-root", type=Path,
        default=Path.home() / ".local/state/life-manager/social-x",
    )
    parser.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(args.source_root, args.target_root, seal=args.seal)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"x-social migration failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
