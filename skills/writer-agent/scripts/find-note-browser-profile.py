#!/usr/bin/env python3
"""Find the newest Camoufox profile without recursively scanning temp roots."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def candidates(root: Path, max_depth: int):
    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            child_depth = depth + 1
            if entry.name.startswith("playwright_firefoxdev_profile-"):
                try:
                    yield (entry.stat(follow_symlinks=False).st_mtime_ns, path)
                except OSError:
                    pass
            if child_depth < max_depth:
                stack.append((path, child_depth))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="root:max_depth",
    )
    parser.add_argument(
        "--ordered-root",
        action="append",
        default=[],
        help="root:max_depth; first root containing a profile wins",
    )
    args = parser.parse_args()
    if not args.root and not args.ordered_root:
        parser.error("at least one --root or --ordered-root is required")
    for item in args.ordered_root:
        root_text, separator, depth_text = item.rpartition(":")
        if not separator:
            raise SystemExit(f"invalid --ordered-root value: {item}")
        found = list(candidates(Path(root_text), int(depth_text)))
        if found:
            print(max(found, key=lambda value: value[0])[1])
            return 0
    found = []
    for item in args.root:
        root_text, separator, depth_text = item.rpartition(":")
        if not separator:
            raise SystemExit(f"invalid --root value: {item}")
        found.extend(candidates(Path(root_text), int(depth_text)))
    if found:
        print(max(found, key=lambda value: value[0])[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
