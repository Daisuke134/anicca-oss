#!/usr/bin/env python3
"""Safely mirror legacy X social state before the Life Manager cutover."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path


MAPPINGS = {
    "x-repost-en": "x-repost/en",
    "x-repost-ja": "x-repost/ja",
    "x-tweeter": "x-tweeter",
}
MARKER = ".legacy-migration.json"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def resolved_non_root(path: Path, label: str) -> Path:
    value = path.expanduser().resolve(strict=False)
    if not value.is_absolute() or value == Path(value.anchor):
        raise ValueError(f"unsafe {label}: {value}")
    return value


def within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def reject_symlinks(root: Path) -> None:
    if root.is_symlink():
        raise ValueError(f"symlink tree is not allowed: {root}")
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"symlink tree is not allowed: {path}")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def stable_copy(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for _ in range(3):
        before = source.stat()
        fd, temporary = tempfile.mkstemp(prefix=".migration.", dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            copied = digest(Path(temporary))
            after = source.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns
            ) or copied != digest(source):
                continue
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            return copied
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    raise RuntimeError(f"source changed repeatedly while copying: {source}")


def migrate(source_root: Path, target_root: Path, *, seal: bool = False) -> dict:
    source_root = resolved_non_root(source_root, "source root")
    target_root = resolved_non_root(target_root, "target root")
    if within(source_root, target_root) or within(target_root, source_root):
        raise ValueError("source and target overlap")
    reject_symlinks(target_root)
    marker_path = target_root / MARKER
    previous: dict = {}
    if marker_path.exists():
        previous = json.loads(marker_path.read_text(encoding="utf-8"))
        if previous.get("version") != 1 or not isinstance(previous.get("files"), dict):
            raise ValueError("invalid migration marker")
        if previous.get("sealed"):
            raise ValueError("migration is sealed; destination is owned by Life Manager")
    elif target_root.exists() and any(target_root.iterdir()):
        raise ValueError("non-empty destination has no migration marker")

    old_files = previous.get("files", {})
    new_files: dict[str, str] = {}
    copied = skipped = 0
    for source_name, target_name in MAPPINGS.items():
        source = source_root / source_name
        if not source.exists():
            continue
        if not source.is_dir():
            raise ValueError(f"legacy source is not a directory: {source}")
        reject_symlinks(source)
        for item in sorted(source.rglob("*")):
            if not item.is_file():
                continue
            relative = Path(target_name) / item.relative_to(source)
            key = relative.as_posix()
            target = target_root / relative
            if target.exists():
                if not target.is_file() or stat.S_ISLNK(target.lstat().st_mode):
                    raise ValueError(f"unsafe migration target: {target}")
                current = digest(target)
                source_digest = digest(item)
                if current == source_digest:
                    skipped += 1
                    new_files[key] = current
                    continue
                if old_files.get(key) != current:
                    raise ValueError(f"destination changed outside migration: {target}")
            new_files[key] = stable_copy(item, target)
            copied += 1

    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in [target_root, *[path for path in target_root.rglob("*") if path.is_dir()]]:
        directory.chmod(0o700)
    atomic_json(marker_path, {
        "version": 1,
        "sealed": bool(seal),
        "source_root": str(source_root),
        "files": new_files,
    })
    return {"copied": copied, "skipped": skipped, "verified": len(new_files), "sealed": bool(seal)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=Path.home() / "loops")
    parser.add_argument(
        "--target-root", type=Path,
        default=Path.home() / ".local/state/life-manager/social-x",
    )
    parser.add_argument("--seal", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(args.source_root, args.target_root, seal=args.seal)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"x-social migration failed: {error}", file=os.sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
