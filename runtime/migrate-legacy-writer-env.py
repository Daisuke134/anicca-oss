#!/usr/bin/env python3
"""Copy allowlisted Writer credentials into the Life Manager environment file."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import stat
from pathlib import Path


KEYS = frozenset({
    "WRITER_PII_BLOCKLIST",
    "WRITER_PII_BLOCKLIST_FILE",
    "DEVTO_API_KEY",
    "SUBSTACK_SESSION_COOKIE",
    "SUBSTACK_SESSION_COOKIE_JA",
    "SUBSTACK_SESSION_COOKIE_EN",
    "NOTE_EMAIL",
    "NOTE_PASSWORD",
    "NOTE_USER_ID",
    "NOTE_URLNAME",
    "NOTE_BROWSER_PROFILE_ID",
    "ZENN_ACCOUNT",
    "DEVTO_ACCOUNT_HANDLE",
    "SUBSTACK_PUBLICATION",
    "SUBSTACK_PUBLICATION_JA",
    "SUBSTACK_PUBLICATION_EN",
    "X_ACCOUNT_HANDLE",
    "ARTICLE_PRODUCT_ID",
    "ARTICLE_PRODUCT_LANDING_URL",
    "ARTICLE_SELF_OWNED_BASE_URL",
    "ARTICLE_INTERNAL_LINK_URLS",
    "ARTICLE_CTA_URLS",
})
LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _reject_symlink_chain(path: Path, label: str) -> None:
    candidate = path.absolute()
    for node in (candidate, *candidate.parents):
        if node.exists() and node.is_symlink():
            raise ValueError(f"{label} has an unsafe symlink path component")


def _parse_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        match = LINE.match(raw.strip())
        if match and match.group(1) in KEYS:
            if match.group(1) in values:
                raise ValueError(f"duplicate allowlisted environment key: {match.group(1)}")
            values[match.group(1)] = match.group(2)
    return values


def _values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"environment path is not a regular file: {path}")
    return _parse_values(path.read_text(encoding="utf-8"))


def migrate(source: Path, target: Path) -> dict[str, int]:
    source = source.expanduser()
    target = target.expanduser()
    _reject_symlink_chain(source, "legacy Writer environment")
    _reject_symlink_chain(target, "Life Manager environment")
    if source.is_symlink() or not source.is_file():
        raise ValueError("legacy Writer environment is unavailable or unsafe")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("Life Manager environment is unsafe")
    source_values = _values(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Life Manager environment is not a regular file")
        handle.seek(0)
        current = handle.read()
        target_values = _parse_values(current)
        conflicts = [key for key in KEYS if key in source_values and key in target_values
                     and source_values[key] != target_values[key]]
        if conflicts:
            raise ValueError("Life Manager environment already has a different allowlisted value")
        additions = [key for key in sorted(KEYS) if key in source_values and key not in target_values]
        if additions:
            separator = "" if not current or current.endswith("\n") else "\n"
            payload = separator + "".join(f"{key}={source_values[key]}\n" for key in additions)
            handle.seek(0, os.SEEK_END)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o600)
    return {
        "copied": len(additions),
        "skipped": sum(key in source_values and key in target_values for key in KEYS),
        "missing": sum(key not in source_values and key not in target_values for key in KEYS),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.home() / ".openclaw/.env")
    parser.add_argument(
        "--target", type=Path, default=Path.home() / ".local/state/life-manager/.env"
    )
    args = parser.parse_args()
    try:
        result = migrate(args.source, args.target)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Writer environment migration failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
