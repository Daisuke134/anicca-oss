#!/usr/bin/env python3
"""Copy only Self Improve runner receipts out of the shared legacy root."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def _safe_root(path: Path, name: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{name} cannot be a symlink")
    value = path.expanduser().resolve(strict=False)
    if value == Path(value.anchor):
        raise ValueError(f"{name} cannot be filesystem root")
    return value


def _safe_dir(root: Path, relative: Path, *, create: bool = False) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"directory cannot be a symlink: {current}")
        if current.exists() and not current.is_dir():
            raise ValueError(f"path component is not a directory: {current}")
        if create and not current.exists():
            current.mkdir(mode=0o700)
        if current.exists():
            os.chmod(current, 0o700)
    return current


def _rewrite_paths(value, source_evidence: Path, target_evidence: Path):
    if isinstance(value, dict):
        return {key: _rewrite_paths(item, source_evidence, target_evidence)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_paths(item, source_evidence, target_evidence) for item in value]
    if isinstance(value, str):
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            try:
                relative = candidate.resolve(strict=False).relative_to(source_evidence)
            except ValueError:
                pass
            else:
                return str(target_evidence / relative)
    return value


def _rows(path: Path, source_evidence: Path, target_evidence: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.is_symlink():
        raise ValueError(f"legacy file cannot be a symlink: {path}")
    result = []
    seen = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("runner_id") == "self-improve":
            row = _rewrite_paths(row, source_evidence, target_evidence)
            run_id = row.get("run_id")
            if run_id in seen and seen[run_id] != row:
                raise ValueError(f"conflicting legacy receipt: {path.name}:{run_id}")
            if run_id not in seen:
                seen[run_id] = row
                result.append(row)
    return result


def migrate(source: Path, target: Path) -> dict[str, int]:
    source = _safe_root(source, "source")
    target = _safe_root(target, "target")
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("source and target cannot overlap")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target, 0o700)
    source_state = _safe_dir(source, Path("state"))
    source_evidence = _safe_dir(source, Path("evidence/runs"))
    target_state = _safe_dir(target, Path("state"), create=True)
    target_evidence = _safe_dir(target, Path("evidence/runs"), create=True)
    copied_rows = 0
    for name in ("run-reports.jsonl", "run-deliveries.jsonl"):
        destination = target_state / name
        if destination.is_symlink():
            raise ValueError(f"target file cannot be a symlink: {destination}")
        rows = _rows(source_state / name, source_evidence, target_evidence)
        existing = _rows(destination, source_evidence, target_evidence)
        known = {row.get("run_id"): row for row in existing}
        for row in rows:
            run_id = row.get("run_id")
            if run_id in known and known[run_id] != row:
                raise ValueError(f"conflicting canonical receipt: {name}:{run_id}")
        missing = [row for row in rows if row.get("run_id") not in known]
        if missing:
            with destination.open("a") as stream:
                for row in missing:
                    stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            os.chmod(destination, 0o600)
            copied_rows += len(missing)

    copied_files = 0
    if source_evidence.exists():
        for item in sorted(source_evidence.glob("*/self-improve/*/*")):
            relative = item.relative_to(source_evidence)
            _safe_dir(source_evidence, relative.parent)
            if item.is_symlink():
                raise ValueError(f"legacy evidence cannot contain symlinks: {item}")
            if not item.is_file():
                continue
            destination_parent = _safe_dir(target_evidence, relative.parent, create=True)
            destination = destination_parent / relative.name
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes() != item.read_bytes():
                    raise ValueError(f"conflicting canonical evidence: {destination}")
                continue
            shutil.copy2(item, destination)
            os.chmod(destination, 0o600)
            copied_files += 1
    source_logs = _safe_dir(source, Path("logs"))
    target_logs = _safe_dir(target, Path("logs"), create=True)
    for name in ("self-improve-evolve-launchd.out.log", "self-improve-evolve-launchd.err.log"):
        item = source_logs / name
        if not item.exists():
            continue
        if item.is_symlink() or not item.is_file():
            raise ValueError(f"legacy log is invalid: {item}")
        destination = target_logs / name
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != item.read_bytes():
                raise ValueError(f"conflicting canonical log: {destination}")
            continue
        shutil.copy2(item, destination)
        os.chmod(destination, 0o600)
        copied_files += 1
    return {"copied_rows": copied_rows, "copied_files": copied_files}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path.home() / ".openclaw")
    parser.add_argument("--target", type=Path, default=Path.home() / ".local/state/life-manager/self-improve-evolve")
    args = parser.parse_args()
    try:
        result = migrate(args.source, args.target)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"self-improve migration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
