#!/usr/bin/env python3
"""Copy Warmup Flip's mutable state and dedicated logs from the legacy root."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def _root(path: Path, name: str) -> Path:
    value = path.expanduser().absolute()
    current = Path(value.anchor)
    for part in value.parts[1:]:
        current /= part
        if current.is_symlink() and current != Path("/var"):
            raise ValueError(f"{name} cannot contain a symlink: {current}")
    if value == Path(value.anchor):
        raise ValueError(f"{name} cannot be filesystem root")
    return value


def _dir(root: Path, relative: str, *, create: bool = False) -> Path:
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"directory cannot be a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise ValueError(f"path component is not a directory: {current}")
        if create and not current.exists():
            current.mkdir(mode=0o700)
        if current.exists():
            os.chmod(current, 0o700)
    return current


def _copy(source: Path, target: Path) -> int:
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"legacy file is invalid: {source}")
    if target.is_symlink():
        raise ValueError(f"conflicting canonical file: {target}")
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise ValueError(f"conflicting canonical file: {target}")
        return 0
    shutil.copy2(source, target)
    os.chmod(target, 0o600)
    return 1


def migrate(source: Path, target: Path) -> dict[str, int]:
    source, target = _root(source, "source"), _root(target, "target")
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("source and target cannot overlap")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    source_state, source_logs = _dir(source, "state"), _dir(source, "logs")
    target_state = _dir(target, "state", create=True)
    target_logs = _dir(target, "logs", create=True)
    state = source_state / "postiz-integrations.json"
    if state.is_symlink() or not state.is_file():
        raise ValueError(f"legacy file is invalid: {state}")
    body = json.loads(state.read_text())
    integrations = body.get("integrations")
    if not isinstance(integrations, list) or not all(isinstance(item, dict) for item in integrations):
        raise ValueError("legacy Postiz integration state is invalid")
    copied = _copy(state, target_state / state.name)
    for name in ("warmup-flip-launchd.out.log", "warmup-flip-launchd.err.log"):
        item = source_logs / name
        if item.is_symlink():
            raise ValueError(f"legacy file is invalid: {item}")
        if item.exists():
            copied += _copy(item, target_logs / name)
    return {"copied_files": copied}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, default=Path.home() / ".local/state/life-manager/warmup-flip-daily")
    args = parser.parse_args()
    try:
        result = migrate(args.source, args.target)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"warmup-flip migration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
