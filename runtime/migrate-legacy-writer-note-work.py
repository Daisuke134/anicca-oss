#!/usr/bin/env python3
"""Copy only live Writer publication work-state out of CloakBrowser storage."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from legacy_state_mirror import mirror_files, resolved_non_root


MARKER = ".legacy-note-work-migration.json"
LIVE_FILES = (
    "draft-ledger.json",
    "note-cookies.json",
    "substack-img-cache.json",
    "thumb.png",
)


def selected_files(source_root: Path) -> dict[Path, Path]:
    selected: dict[Path, Path] = {}
    for name in LIVE_FILES:
        path = source_root / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"legacy Writer work item is unsafe: {name}")
        selected[Path(name)] = Path(name)
    return selected


def migrate(source_root: Path, target_root: Path, *, seal: bool = False) -> dict:
    source_root = resolved_non_root(source_root, "legacy Writer note-work")
    return mirror_files(
        source_root,
        target_root,
        selected_files(source_root),
        marker_name=MARKER,
        seal=seal,
        allow_existing_target=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root", type=Path, default=Path.home() / ".cloak/note-work"
    )
    parser.add_argument(
        "--target-root",
        type=Path,
        default=Path.home() / ".local/state/life-manager/writer/note-work",
    )
    parser.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(args.source_root, args.target_root, seal=args.seal)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"Writer note-work migration failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
