#!/usr/bin/env python3
"""Persist portable Zenn publication settings in Life Manager's private env."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import stat
from pathlib import Path


LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
KEYS = (
    "ZENN_ACCOUNT",
    "ZENN_REPOSITORY_URL",
    "ZENN_GIT_NAME",
    "ZENN_GIT_EMAIL",
    "ARTICLE_MEDIA_RAW_BASE",
)


def _reject_symlinks(path: Path) -> None:
    candidate = path.absolute()
    for node in (candidate, *candidate.parents):
        if node.exists() and node.is_symlink():
            raise ValueError("Life Manager environment has an unsafe symlink component")


def _parse(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            words = shlex.split(stripped, posix=True)
        except ValueError as error:
            raise ValueError("invalid shell syntax in Life Manager environment") from error
        if len(words) != 1:
            continue
        match = LINE.match(words[0])
        if match:
            if match.group(1) in result:
                raise ValueError(f"duplicate environment key: {match.group(1)}")
            result[match.group(1)] = match.group(2)
    return result


def configure(target: Path, values: dict[str, str]) -> dict[str, int]:
    target = target.expanduser()
    _reject_symlinks(target)
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise ValueError("Life Manager environment is unsafe")
    if re.fullmatch(r"[A-Za-z0-9_-]+", values["ZENN_ACCOUNT"]) is None:
        raise ValueError("invalid Zenn account")
    if not values["ZENN_REPOSITORY_URL"].strip():
        raise ValueError("Zenn repository URL is required")
    if not values["ZENN_GIT_NAME"].strip():
        raise ValueError("Zenn Git name is required")
    if re.fullmatch(r"[^@\s]+@[^@\s]+", values["ZENN_GIT_EMAIL"]) is None:
        raise ValueError("invalid Zenn Git email")
    if not re.fullmatch(r"https://raw\.githubusercontent\.com/.+/images", values["ARTICLE_MEDIA_RAW_BASE"]):
        raise ValueError("invalid Zenn media raw base")

    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Life Manager environment is not a regular file")
        handle.seek(0)
        current_text = handle.read()
        current = _parse(current_text)
        conflicts = [key for key in KEYS if key in current and current[key] != values[key]]
        if conflicts:
            raise ValueError("Life Manager environment has conflicting Zenn settings")
        additions = [key for key in KEYS if key not in current]
        if additions:
            separator = "" if not current_text or current_text.endswith("\n") else "\n"
            handle.seek(0, os.SEEK_END)
            handle.write(
                separator
                + "".join(f"{key}={shlex.quote(values[key])}\n" for key in additions)
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o600)
    return {"configured": len(additions), "skipped": len(KEYS) - len(additions)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, default=Path.home() / ".local/state/life-manager/.env")
    parser.add_argument("--account", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--git-name", required=True)
    parser.add_argument("--git-email", required=True)
    parser.add_argument("--media-raw-base", required=True)
    args = parser.parse_args()
    values = {
        "ZENN_ACCOUNT": args.account,
        "ZENN_REPOSITORY_URL": args.repository_url,
        "ZENN_GIT_NAME": args.git_name,
        "ZENN_GIT_EMAIL": args.git_email,
        "ARTICLE_MEDIA_RAW_BASE": args.media_raw_base.rstrip("/"),
    }
    try:
        result = configure(args.target, values)
    except (OSError, ValueError) as error:
        print(f"Zenn configuration failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
