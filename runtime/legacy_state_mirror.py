"""Shared, restart-safe mirror for legacy state cutovers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def resolved_non_root(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ValueError(f"symlink {label} is not allowed: {expanded}")
    value = expanded.resolve(strict=False)
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


def validate_target_parents(target_root: Path, target: Path) -> None:
    parent = target.parent
    while within(target_root, parent):
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise ValueError(f"unsafe migration target parent: {parent}")
        if parent == target_root:
            return
        parent = parent.parent
    raise ValueError(f"migration target escapes target root: {target}")


def validate_source_path(source_root: Path, source: Path) -> None:
    if not within(source_root, source):
        raise ValueError(f"migration source escapes source root: {source}")
    current = source
    while within(source_root, current):
        if current.is_symlink():
            raise ValueError(f"symlink migration source is not allowed: {current}")
        if current == source_root:
            break
        current = current.parent
    if not source.is_file():
        raise ValueError(f"migration source is not a regular file: {source}")


def atomic_json(path: Path, value: dict, confined_root: Path | None = None) -> None:
    if confined_root is not None:
        validate_target_parents(confined_root, path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        if confined_root is not None:
            validate_target_parents(confined_root, path)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def stable_digest(source_root: Path, path: Path) -> str:
    for _ in range(3):
        validate_source_path(source_root, path)
        before = path.stat()
        value = digest(path)
        after = path.stat()
        validate_source_path(source_root, path)
        if (before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_ino, after.st_size, after.st_mtime_ns
        ):
            return value
    raise RuntimeError(f"file changed repeatedly while reading: {path}")


def stable_copy(
    source: Path,
    source_root: Path,
    target_root: Path,
    target: Path,
    expected_target_digest: str | None,
) -> str:
    validate_target_parents(target_root, target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for _ in range(3):
        validate_source_path(source_root, source)
        before = source.stat()
        fd, temporary = tempfile.mkstemp(prefix=".migration.", dir=target.parent)
        os.close(fd)
        try:
            shutil.copyfile(source, temporary)
            copied = digest(Path(temporary))
            after = source.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns
            ) or copied != stable_digest(source_root, source):
                continue
            validate_target_parents(target_root, target)
            if expected_target_digest is None:
                if target.exists() or target.is_symlink():
                    raise ValueError(f"migration target appeared during copy: {target}")
            else:
                if not target.is_file() or target.is_symlink() \
                        or digest(target) != expected_target_digest:
                    raise ValueError(f"migration target changed during copy: {target}")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            return copied
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    raise RuntimeError(f"source changed repeatedly while copying: {source}")


def mirror_files(
    source_root: Path,
    target_root: Path,
    files: dict[Path, Path],
    *,
    marker_name: str,
    seal: bool = False,
    allow_existing_target: bool = False,
) -> dict:
    source_root = resolved_non_root(source_root, "source root")
    target_root = resolved_non_root(target_root, "target root")
    if within(source_root, target_root) or within(target_root, source_root):
        raise ValueError("source and target overlap")
    marker_path = target_root / marker_name
    has_marker = marker_path.exists()
    previous: dict = {}
    if has_marker:
        previous = json.loads(marker_path.read_text(encoding="utf-8"))
        if previous.get("version") != 1 or not isinstance(previous.get("files"), dict):
            raise ValueError("invalid migration marker")
        if previous.get("source_root") != str(source_root):
            raise ValueError("migration source root changed")
        if previous.get("sealed"):
            raise ValueError("migration is sealed; destination is owned by Life Manager")
    elif target_root.exists() and any(target_root.iterdir()) and not allow_existing_target:
        raise ValueError("non-empty destination has no migration marker")

    old_files = previous.get("files", {})
    prepared_files: dict[str, str] = {}
    created_directories: set[Path] = set()
    for target_relative in files.values():
        parent = target_root / target_relative.parent
        while within(target_root, parent) and not parent.exists():
            created_directories.add(parent)
            if parent == target_root:
                break
            parent = parent.parent
    target_keys = [path.as_posix() for path in files.values()]
    if len(target_keys) != len(set(target_keys)):
        raise ValueError("migration target mapping is not unique")

    target_expectations: dict[str, str | None] = {}
    for source_relative, target_relative in sorted(files.items(), key=lambda pair: pair[1].as_posix()):
        if source_relative.is_absolute() or target_relative.is_absolute() \
                or ".." in source_relative.parts or ".." in target_relative.parts:
            raise ValueError("migration mapping must be relative and contained")
        source = source_root / source_relative
        target = target_root / target_relative
        validate_target_parents(target_root, target)
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"migration source is not a regular file: {source}")
        key = target_relative.as_posix()
        source_digest = stable_digest(source_root, source)
        if target.is_symlink():
            raise ValueError(f"unsafe migration target: {target}")
        if target.exists():
            if not target.is_file() or stat.S_ISLNK(target.lstat().st_mode):
                raise ValueError(f"unsafe migration target: {target}")
            current = digest(target)
            if not has_marker:
                raise ValueError(f"existing migration target has no ownership marker: {target}")
            if current != source_digest and old_files.get(key) != current:
                raise ValueError(f"destination changed outside migration: {target}")
            prepared_files[key] = current
            target_expectations[key] = current
        else:
            prepared_files[key] = source_digest
            target_expectations[key] = None

    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in sorted(created_directories, key=lambda path: len(path.parts)):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    atomic_json(marker_path, {
        "version": 1,
        "sealed": False,
        "source_root": str(source_root),
        "files": prepared_files,
    }, target_root)

    new_files: dict[str, str] = {}
    copied = skipped = 0
    for source_relative, target_relative in sorted(files.items(), key=lambda pair: pair[1].as_posix()):
        source = source_root / source_relative
        target = target_root / target_relative
        key = target_relative.as_posix()
        source_digest = stable_digest(source_root, source)
        expected_target = target_expectations[key]
        if expected_target is not None:
            if not target.is_file() or target.is_symlink() \
                    or digest(target) != expected_target:
                raise ValueError(f"migration target changed after preflight: {target}")
        elif target.exists() or target.is_symlink():
            raise ValueError(f"migration target appeared after preflight: {target}")
        if expected_target == source_digest:
            skipped += 1
            new_files[key] = source_digest
            continue
        if expected_target is not None and not seal:
            raise ValueError(
                f"legacy source changed after pre-copy; update requires sealed idle cutover: {source}"
            )
        new_files[key] = stable_copy(source, source_root, target_root, target, expected_target)
        copied += 1

    validate_target_parents(target_root, marker_path)
    atomic_json(marker_path, {
        "version": 1,
        "sealed": bool(seal),
        "source_root": str(source_root),
        "files": new_files,
    }, target_root)
    return {"copied": copied, "skipped": skipped, "verified": len(new_files), "sealed": bool(seal)}
