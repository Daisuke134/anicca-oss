#!/usr/bin/env python3
"""Copy the Lateness loop's required mutable data out of the legacy home."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _root(path: Path, name: str) -> Path:
    if ".." in path.expanduser().parts:
        raise ValueError(f"{name} cannot contain '..'")
    value = path.expanduser().absolute()
    current = Path(value.anchor)
    for part in value.parts[1:]:
        current /= part
        if current.is_symlink() and current != Path("/var"):
            raise ValueError(f"{name} cannot contain a symlink: {current}")
    if value == Path(value.anchor):
        raise ValueError(f"{name} cannot be filesystem root")
    return value


def _ensure_dir(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink() and current != Path("/var"):
            raise ValueError(f"directory cannot be a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise ValueError(f"path component is not a directory: {current}")
        if not current.exists():
            current.mkdir(mode=0o700)
    path.chmod(0o700)


def _copy(source: Path, target: Path) -> int:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"legacy file is invalid: {source}")
    _ensure_dir(target.parent)
    if target.is_symlink():
        raise ValueError(f"conflicting canonical file: {target}")
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise ValueError(f"conflicting canonical file: {target}")
        target.chmod(0o600)
        return 0
    shutil.copy2(source, target)
    target.chmod(0o600)
    return 1


def _atomic_marker(path: Path, result: dict[str, int]) -> None:
    _ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(result, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def migrate(source: Path, target_home: Path, target_loop_root: Path) -> dict[str, int]:
    source = _root(source, "source")
    target_home = _root(target_home, "target home")
    target_loop_root = _root(target_loop_root, "target loop root")
    for target in (target_home, target_loop_root):
        if source == target or source in target.parents or target in source.parents:
            raise ValueError("source and target cannot overlap")

    copied = 0
    optional = (
        (source / "identity/profile.json", target_home / "identity/profile.json"),
    )
    for old, new in optional:
        if old.is_symlink():
            raise ValueError(f"legacy file is invalid: {old}")
        if old.exists():
            copied += _copy(old, new)

    location_source = source / "state/location"
    if location_source.is_symlink():
        raise ValueError(f"legacy directory is invalid: {location_source}")
    if location_source.exists():
        if not location_source.is_dir():
            raise ValueError(f"legacy directory is invalid: {location_source}")
        for old in sorted(location_source.glob("*.json")):
            copied += _copy(old, target_home / "state/location" / old.name)

    optional = (
        (source / "state/call-bridge/public_url.txt", target_home / "state/call-bridge/public_url.txt"),
        (source / "state/arrival/notified.json", target_home / "state/arrival/notified.json"),
        (source / "skills/anicca-life-manager/state/run.log", target_loop_root / "logs/run.log"),
        (source / "skills/anicca-life-manager/state/heartbeat_log.jsonl", target_loop_root / "state/heartbeat_log.jsonl"),
        (source / "skills/anicca-life-manager/state/nudge_sent.json", target_loop_root / "state/nudge_sent.json"),
        (source / "skills/anicca-life-manager/state/active_call_loop.json", target_loop_root / "state/active_call_loop.json"),
        (source / "skills/anicca-life-manager/state/renraku_sent.json", target_loop_root / "state/renraku_sent.json"),
    )
    for old, new in optional:
        if old.is_symlink():
            raise ValueError(f"legacy file is invalid: {old}")
        if old.exists():
            copied += _copy(old, new)

    result = {"copied_files": copied}
    _atomic_marker(target_loop_root / "legacy-migration.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.home() / ".openclaw")
    parser.add_argument("--target-home", type=Path, default=Path.home() / ".local/state/life-manager")
    parser.add_argument("--target-loop-root", type=Path, default=Path.home() / ".local/state/life-manager/lateness-heartbeat")
    args = parser.parse_args()
    try:
        result = migrate(args.source, args.target_home, args.target_loop_root)
    except (OSError, ValueError) as error:
        print(f"lateness migration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
