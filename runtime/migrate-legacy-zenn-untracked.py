#!/usr/bin/env python3
"""Preserve untracked legacy Zenn files outside the clean managed checkout."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from legacy_state_mirror import mirror_files, reject_symlinks, resolved_non_root


MARKER = ".legacy-zenn-untracked-migration.json"


def selected_files(source: Path) -> dict[Path, Path]:
    if not (source / ".git").is_dir():
        raise ValueError("legacy Zenn source is not a Git checkout")
    result = subprocess.run(
        ["git", "-C", str(source), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("cannot enumerate legacy Zenn untracked files")
    selected: dict[Path, Path] = {}
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        source_file = source / relative
        if source_file.is_symlink() or not source_file.is_file():
            raise ValueError(f"unsafe legacy Zenn untracked path: {relative}")
        selected[relative] = Path("legacy-zenn-untracked") / relative
    reject_symlinks(source)
    return selected


def migrate(source: Path, target: Path, *, seal: bool = False) -> dict:
    source = resolved_non_root(source, "legacy Zenn source")
    return mirror_files(
        source,
        target,
        selected_files(source),
        marker_name=MARKER,
        seal=seal,
        allow_existing_target=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.home() / ".openclaw/workspace/zenn-articles",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=Path.home() / ".local/state/life-manager/writer",
    )
    parser.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(args.source, args.target, seal=args.seal)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"legacy Zenn migration failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
