#!/usr/bin/env python3
"""Copy the allowlisted Writer content context into Life Manager state."""

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


MARKER = ".legacy-writer-content-migration.json"
LIBRARY_NAMES = {
    "account-history.jsonl",
    "pattern-article.jsonl",
    "pattern-card-en.jsonl",
    "pattern-card-ja.jsonl",
    "pattern-iam-en.jsonl",
    "pattern-iam-ja.jsonl",
    "pattern-x.jsonl",
    "pattern-yt-long.jsonl",
    "verbatim_blacklist.txt",
}


def _selected_regular_files(root: Path, names: set[str] | None = None) -> list[Path]:
    if root.is_symlink():
        raise ValueError(f"symlink legacy Writer content is not allowed: {root}")
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"legacy Writer content is not a directory: {root}")
    files = []
    for item in sorted(root.iterdir()):
        if names is not None and item.name not in names:
            continue
        if names is None and item.suffix != ".jsonl":
            continue
        if item.is_symlink() or not item.is_file():
            raise ValueError(f"legacy Writer content is not a regular file: {item}")
        files.append(item)
    return files


def selected_files(source_root: Path) -> dict[Path, Path]:
    files: dict[Path, Path] = {}
    library = source_root / "state/content-library"
    for item in _selected_regular_files(library, LIBRARY_NAMES):
        files[item.relative_to(source_root)] = Path("content-library") / item.name

    experience = source_root / "state/experience-log"
    for item in _selected_regular_files(experience):
        files[item.relative_to(source_root)] = Path("experience-log") / item.name

    persona = source_root / "skills/anicca-persona/persona-anicca.md"
    if persona.exists() or persona.is_symlink():
        if persona.is_symlink() or not persona.is_file():
            raise ValueError(f"legacy Writer persona is not a regular file: {persona}")
        files[persona.relative_to(source_root)] = Path("config/persona.md")
    return files


def migrate(source_root: Path, target_root: Path, *, seal: bool = False) -> dict:
    source_root = resolved_non_root(source_root, "source root")
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
    parser.add_argument("--source-root", type=Path, default=Path.home() / ".openclaw")
    parser.add_argument(
        "--target-root",
        type=Path,
        default=Path.home() / ".local/state/life-manager/writer",
    )
    parser.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(args.source_root, args.target_root, seal=args.seal)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"Writer content migration failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
