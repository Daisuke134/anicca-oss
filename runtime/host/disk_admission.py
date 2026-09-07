#!/usr/bin/env python3
"""Fail closed before a Life Manager producer allocates disk-backed work."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Sequence


DEFAULT_REQUIRED_KIB = 524288
REQUIRED_KIB = int(
    os.environ.get("LIFE_MANAGER_DISK_HEADROOM_KIB", str(DEFAULT_REQUIRED_KIB))
)
REQUIRED_BYTES = REQUIRED_KIB * 1024
RECEIPT_PATH = Path("state") / "disk-headroom.json"

_PRODUCER_GATE = "life-manager-producer-preflight"
_POLICY_FLAGS = (
    ("disk-writers.stop", "disk_writers_stop"),
    ("disk-pressure.block", "disk_pressure_block"),
)


def _state_dir() -> Path:
    configured = os.environ.get("LIFE_MANAGER_PRODUCER_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "life-manager"


def _ensure_producer_state_dir(state_dir: Path) -> bool:
    """Prepare the receipt/filesystem root before measuring a fresh instance."""
    try:
        if not state_dir.exists():
            state_dir.mkdir(parents=True, mode=0o700)
            state_dir.chmod(0o700)
        entry = state_dir.lstat()
        return (
            stat.S_ISDIR(entry.st_mode)
            and not state_dir.is_symlink()
            and entry.st_uid == os.getuid()
            and not entry.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
    except OSError:
        return False


def _host_state_dir() -> Path:
    configured = os.environ.get("LIFE_MANAGER_HOST_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "life-manager" / "state"


def _ensure_host_state_dir(host_state: Path) -> bool:
    """Create a fresh private control root, rejecting unsafe existing paths."""
    try:
        if not host_state.exists():
            host_state.mkdir(parents=True, mode=0o700)
            host_state.chmod(0o700)
        entry = host_state.lstat()
        return (
            stat.S_ISDIR(entry.st_mode)
            and not host_state.is_symlink()
            and entry.st_uid == os.getuid()
            and not entry.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
    except OSError:
        return False


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(str(directory), flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_receipt(state_dir: Path, receipt: dict[str, object]) -> str:
    destination = state_dir / RECEIPT_PATH
    payload = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temporary: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=".disk-headroom.", suffix=".tmp", dir=destination.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    except Exception as exc:
        print(f"disk_admission: could not persist receipt: {exc}", file=sys.stderr)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return payload


def _failure(
    reason: str,
    available_bytes: int | None,
    *,
    metadata: dict[str, object] | None = None,
) -> int:
    receipt: dict[str, object] = {
        "status": "failed",
        "failed": 1,
        "effect": 0,
        "readback": 0,
        "reason": reason,
        "required_bytes": REQUIRED_BYTES,
    }
    if available_bytes is not None:
        receipt["available_bytes"] = available_bytes
    if metadata:
        receipt.update(metadata)
    payload = _write_receipt(_state_dir(), receipt)
    print(payload)
    return 1


def _producer_gate() -> tuple[str, Path] | None:
    host_state = _host_state_dir()
    try:
        if not _ensure_host_state_dir(host_state):
            return "disk_policy_unavailable", host_state
        next(iter(host_state.iterdir()), None)
    except OSError:
        return "disk_policy_unavailable", host_state
    ignored = {
        "disk-writers.stop": os.environ.get(
            "LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP", ""
        ).strip().lower() in {"1", "true", "yes"},
        "disk-pressure.block": os.environ.get(
            "LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK", ""
        ).strip().lower() in {"1", "true", "yes"},
    }
    for filename, reason in _POLICY_FLAGS:
        if ignored[filename]:
            continue
        flag = host_state / filename
        try:
            entry = flag.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return "disk_policy_unavailable", flag
        if not stat.S_ISREG(entry.st_mode):
            return "disk_policy_unavailable", flag
        return reason, flag
    return None


def disk_headroom_ok() -> bool:
    state_dir = _state_dir()
    if not _ensure_producer_state_dir(state_dir):
        print(json.dumps({
            "status": "failed", "failed": 1, "effect": 0, "readback": 0,
            "reason": "disk_state_unsafe", "required_bytes": REQUIRED_BYTES,
        }, sort_keys=True, separators=(",", ":")))
        return False
    gate = _producer_gate()
    if gate is not None:
        reason, flag = gate
        try:
            available_bytes = int(shutil.disk_usage(state_dir).free)
        except Exception:
            available_bytes = None
        _failure(
            reason,
            available_bytes,
            metadata={"gate": _PRODUCER_GATE, "flag_path": str(flag)},
        )
        return False
    try:
        available_bytes = int(shutil.disk_usage(state_dir).free)
    except Exception:
        _failure("disk_headroom_unavailable", None)
        return False
    if REQUIRED_BYTES and available_bytes < REQUIRED_BYTES:
        _failure("disk_headroom_low", available_bytes)
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    remaining = list(sys.argv[1:] if argv is None else argv)
    if not remaining:
        print("disk_admission: missing child argv", file=sys.stderr)
        return 2
    if not disk_headroom_ok():
        return 1
    os.execvpe(remaining[0], remaining, os.environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
