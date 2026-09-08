#!/usr/bin/env python3
"""Safely mirror allowlisted legacy Writer state and logs before cutover."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parent
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from legacy_state_mirror import mirror_files, reject_symlinks, resolved_non_root


MARKER = ".legacy-writer-migration.json"
STATE_PREFIXES = (
    ".article-",
    ".self-fix-article-writer",
    ".self-fix-writer-agent",
)
LOG_PREFIXES = ("article-", "writer-")
LOG_NAMES = {"note-eyecatch.log", "self-fix-writer-agent.log"}


def selected_files(source_root: Path) -> dict[Path, Path]:
    files: dict[Path, Path] = {}
    state_root = source_root / "state"
    if state_root.is_symlink():
        raise ValueError(f"symlink legacy Writer state is not allowed: {state_root}")
    if state_root.exists():
        if not state_root.is_dir() or state_root.is_symlink():
            raise ValueError(f"legacy Writer state is not a directory: {state_root}")
        for item in sorted(state_root.iterdir()):
            if item.name.startswith(STATE_PREFIXES):
                if not item.is_file() or item.is_symlink():
                    raise ValueError(f"legacy Writer state is not a regular file: {item}")
                files[item.relative_to(source_root)] = Path(item.name)

    logs_root = source_root / "logs"
    if logs_root.is_symlink():
        raise ValueError(f"symlink legacy Writer logs is not allowed: {logs_root}")
    if logs_root.exists():
        if not logs_root.is_dir() or logs_root.is_symlink():
            raise ValueError(f"legacy Writer logs is not a directory: {logs_root}")
        for item in sorted(logs_root.iterdir()):
            if item.name == "article-writer":
                continue
            selected = item.name.startswith(LOG_PREFIXES) or item.name in LOG_NAMES
            if selected and (not item.is_file() or item.is_symlink()):
                raise ValueError(f"legacy Writer log is not a regular file: {item}")
            if selected:
                files[item.relative_to(source_root)] = Path("logs") / item.name
        article_logs = logs_root / "article-writer"
        if article_logs.is_symlink():
            raise ValueError(f"symlink legacy article Writer logs is not allowed: {article_logs}")
        if article_logs.exists():
            if not article_logs.is_dir():
                raise ValueError(f"legacy article Writer logs is not a directory: {article_logs}")
            reject_symlinks(article_logs)
            for item in sorted(article_logs.rglob("*")):
                if item.is_file():
                    files[item.relative_to(source_root)] = (
                        Path("logs/article-writer") / item.relative_to(article_logs)
                    )

    seo_root = source_root / "skills/anicca-seo-rank-monitor/state"
    if seo_root.is_symlink():
        raise ValueError(f"symlink legacy Writer SEO state is not allowed: {seo_root}")
    if seo_root.exists():
        if not seo_root.is_dir():
            raise ValueError(f"legacy Writer SEO state is not a directory: {seo_root}")
        reject_symlinks(seo_root)
        for item in sorted(seo_root.rglob("*")):
            if item.is_file():
                files[item.relative_to(source_root)] = (
                    Path("seo-rank-monitor") / item.relative_to(seo_root)
                )
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
        print(f"Writer migration failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
