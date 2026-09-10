#!/usr/bin/env python3
"""Copy legacy X402 business state into Life Manager without deleting its source."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def require_plain_dir(path: Path, *, create: bool = False) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlink directory: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    path.chmod(0o700)


def require_plain_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"refusing non-regular file: {path}")


def atomic_copy(source: Path, target: Path) -> None:
    require_plain_file(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if digest(source) != digest(target):
        raise RuntimeError(f"hash verification failed: {source.name}")


def sqlite_logical_digest(path: Path) -> str:
    value = hashlib.sha256()
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
        if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError(f"SQLite integrity check failed: {path.name}")
        for statement in database.iterdump():
            value.update(statement.encode("utf-8"))
            value.update(b"\n")
    return value.hexdigest()


def sqlite_logical_statements(path: Path) -> frozenset[str]:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as database:
        if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError(f"SQLite integrity check failed: {path.name}")
        return frozenset(database.iterdump())


def cleanup_sqlite_artifacts(path: Path) -> None:
    path.unlink(missing_ok=True)
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)


def copy_flat_state(source_root: Path, target_root: Path, *, sealed: bool) -> tuple[int, int]:
    if not source_root.exists():
        return 0, 0
    require_plain_dir(source_root)
    require_plain_dir(target_root, create=True)
    copied = skipped = 0
    for source in sorted(source_root.iterdir()):
        if source.suffix not in {".json", ".jsonl"}:
            continue
        require_plain_file(source)
        target = target_root / source.name
        if target.exists() or target.is_symlink():
            require_plain_file(target)
            if digest(source) == digest(target):
                target.chmod(0o600)
                skipped += 1
                continue
            if not sealed:
                raise RuntimeError(f"target differs during precopy: {target.name}")
            if source.suffix == ".jsonl":
                source_bytes = source.read_bytes()
                target_bytes = target.read_bytes()
                if target_bytes.startswith(source_bytes):
                    target.chmod(0o600)
                    skipped += 1
                    continue
                if not source_bytes.startswith(target_bytes):
                    raise RuntimeError(f"append-only histories diverge: {target.name}")
            elif target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
                target.chmod(0o600)
                skipped += 1
                continue
        atomic_copy(source, target)
        copied += 1
    return copied, skipped


def backup_sqlite(source: Path, target: Path, *, sealed: bool) -> tuple[int, int]:
    if not source.exists():
        return 0, 0
    require_plain_file(source)
    require_plain_dir(target.parent, create=True)
    target_exists = target.exists() or target.is_symlink()
    target_sidecars = (Path(f"{target}-wal"), Path(f"{target}-shm"))
    if not target_exists and any(path.exists() or path.is_symlink() for path in target_sidecars):
        raise RuntimeError(f"orphan target SQLite sidecars present: {target.name}")
    if target_exists:
        require_plain_file(target)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as old_database:
            with closing(sqlite3.connect(temporary)) as new_database:
                old_database.backup(new_database)
                result = new_database.execute("PRAGMA integrity_check").fetchone()
                if result != ("ok",):
                    raise RuntimeError("SQLite backup integrity check failed")
        if target_exists:
            same_content = sqlite_logical_digest(temporary) == sqlite_logical_digest(target)
            if not sealed and not same_content:
                raise RuntimeError(f"target differs during precopy: {target.name}")
            if same_content:
                target.chmod(0o600)
                return 0, 1
            source_statements = sqlite_logical_statements(temporary)
            target_statements = sqlite_logical_statements(target)
            if source_statements < target_statements:
                target.chmod(0o600)
                return 0, 1
            if not target_statements < source_statements:
                raise RuntimeError(f"SQLite histories diverge: {target.name}")
            if any(path.exists() or path.is_symlink() for path in target_sidecars):
                raise RuntimeError(f"target SQLite sidecars present: {target.name}")
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        cleanup_sqlite_artifacts(temporary)
    return 1, 0


def migrate(args: argparse.Namespace) -> dict[str, int | str]:
    sealed = args.mode == "converge"
    if sealed and not args.source_sealed:
        raise ValueError("converge requires --source-sealed")
    x402_source = Path(args.x402_source).expanduser()
    the402_source = Path(args.the402_sqlite).expanduser()
    target_root = Path(args.target_root).expanduser()
    if not target_root.is_absolute():
        raise ValueError("target root must be absolute")
    files_copied, files_skipped = copy_flat_state(x402_source, target_root, sealed=sealed)
    sqlite_copied, sqlite_skipped = backup_sqlite(
        the402_source,
        target_root / "the402-inbox.sqlite",
        sealed=sealed,
    )
    return {
        "mode": args.mode,
        "files_copied": files_copied,
        "files_skipped": files_skipped,
        "sqlite_copied": sqlite_copied,
        "sqlite_skipped": sqlite_skipped,
    }


def parser() -> argparse.ArgumentParser:
    home = Path.home()
    result = argparse.ArgumentParser()
    result.add_argument("--mode", choices=("precopy", "converge"), default="precopy")
    result.add_argument("--source-sealed", action="store_true")
    result.add_argument("--x402-source", default=str(home / "anicca/skills/earn/x402-sell/state"))
    result.add_argument("--the402-sqlite", default=str(home / ".anicca/the402-inbox.sqlite"))
    result.add_argument("--target-root", default=str(home / ".local/state/life-manager/x402-sell"))
    return result


def main() -> None:
    import json

    print(json.dumps(migrate(parser().parse_args()), sort_keys=True))


if __name__ == "__main__":
    main()
