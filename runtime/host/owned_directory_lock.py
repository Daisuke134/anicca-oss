#!/usr/bin/env python3
"""macOS/Linux PID/start-identity directory lock for bounded loop passes."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time


OWNER_FILE = "owner.json"
INITIALIZING_GRACE_SECONDS = 30


def process_start(pid: int) -> str | None:
    if pid <= 0:
        return None
    ps = shutil.which("ps")
    if not ps:
        return None
    result = subprocess.run(
        [ps, "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def owner(lock: Path) -> dict[str, object] | None:
    try:
        owner_path = lock / OWNER_FILE if lock.is_dir() else lock
        value = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    if set(value) != {"version", "pid", "process_start", "token"}:
        return None
    if value["version"] != 1 or not isinstance(value["pid"], int) or value["pid"] <= 0:
        return None
    if not isinstance(value["process_start"], str) or not value["process_start"]:
        return None
    if not isinstance(value["token"], str) or not value["token"]:
        return None
    return value


def live(value: dict[str, object] | None) -> bool:
    return bool(value and process_start(value["pid"]) == value["process_start"])


def prepare_owner(lock: Path, pid: int, token: str) -> Path:
    start = process_start(pid)
    if not start or not token:
        raise RuntimeError("owner identity unavailable")
    temporary = lock.parent / f".{lock.name}.owner.{secrets.token_hex(8)}.tmp"
    payload = {"version": 1, "pid": pid, "process_start": start, "token": token}
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def inspect_and_reclaim(lock: Path) -> str:
    """Serialize stale replacement on the observed inode and defeat ABA races."""
    try:
        descriptor = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return "retry"
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        observed = os.fstat(descriptor)
        try:
            current = os.lstat(lock)
        except FileNotFoundError:
            return "retry"
        if (observed.st_dev, observed.st_ino) != (current.st_dev, current.st_ino):
            return "retry"
        recorded_owner = owner(lock)
        if live(recorded_owner) or (
            lock.is_dir() and recorded_owner is None
            and time.time() - current.st_mtime < INITIALIZING_GRACE_SECONDS
        ):
            return "busy"
        quarantine = lock.with_name(f"{lock.name}.stale.{secrets.token_hex(8)}")
        lock.rename(quarantine)
        if quarantine.is_dir():
            try:
                (quarantine / OWNER_FILE).unlink()
            except FileNotFoundError:
                pass
            try:
                quarantine.rmdir()
            except OSError:
                pass
        else:
            quarantine.unlink()
        return "reclaimed"
    finally:
        os.close(descriptor)


def acquire(lock: Path, pid: int, token: str) -> int:
    if not lock.is_absolute() or lock == Path(lock.anchor):
        raise RuntimeError("lock path unavailable")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(lock.parent, 0o700)
    temporary = prepare_owner(lock, pid, token)
    reclaimed = False
    try:
        while True:
            try:
                os.link(temporary, lock)
                os.chmod(lock, 0o600)
                break
            except FileExistsError:
                if lock.is_symlink() or not (lock.is_file() or lock.is_dir()):
                    raise RuntimeError("lock path unavailable")
                disposition = inspect_and_reclaim(lock)
                if disposition == "busy":
                    print('{"status":"busy"}')
                    return 75
                if disposition == "retry":
                    continue
                reclaimed = True
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"status": "reclaimed" if reclaimed else "acquired"}, separators=(",", ":")))
    return 0


def release(lock: Path, pid: int, token: str) -> int:
    if lock.is_symlink() or not lock.is_file():
        raise RuntimeError("lock ownership mismatch")
    value = owner(lock)
    if not value or value["pid"] != pid or value["token"] != token:
        raise RuntimeError("lock ownership mismatch")
    if value["process_start"] != process_start(pid):
        raise RuntimeError("lock ownership mismatch")
    lock.unlink()
    print('{"status":"released"}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("acquire", "release"))
    parser.add_argument("lock", type=Path)
    parser.add_argument("pid", type=int)
    parser.add_argument("token")
    args = parser.parse_args()
    try:
        if args.action == "acquire":
            return acquire(args.lock, args.pid, args.token)
        return release(args.lock, args.pid, args.token)
    except (OSError, RuntimeError, ValueError):
        print('{"status":"error"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
