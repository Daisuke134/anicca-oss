#!/usr/bin/env python3
"""Stop a gig lane before it can allocate work when disk headroom is low."""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Sequence


# skills/earn/gig/TODO.md documents 524,288 KiB (512 MiB) as the last-resort disk-headroom
# floor every gig lane is meant to run under. A guard whose default is "no floor" is not a
# guard: a lane whose plist never carries GIG_DISK_HEADROOM_KIB -- because it migrated onto
# lm-loop's registry (config/loop-registry.json), whose rendered plist never sets this key --
# gets exactly this fallback and nothing else. It must be the same 512 MiB every launchd-jobs.json
# lane sets explicitly, not zero.
DEFAULT_REQUIRED_KIB = 524288
REQUIRED_KIB = int(os.environ.get("GIG_DISK_HEADROOM_KIB", str(DEFAULT_REQUIRED_KIB)))
REQUIRED_BYTES = REQUIRED_KIB * 1024
RECEIPT_PATH = Path("state") / "disk-headroom.json"

_PRODUCER_GATE = "life-manager-producer-preflight"
_POLICY_FLAGS = (
    ("disk-writers.stop", "disk_writers_stop"),
    ("disk-pressure.block", "disk_pressure_block"),
)


def _state_dir() -> Path:
    return Path(os.environ.get("GIG_STATE_DIR") or (Path.home() / "gig"))


def _fsync_directory(directory: Path) -> None:
    """Flush the directory entry after replacing a receipt, then close the fd."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(str(directory), flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_receipt(state_dir: Path, receipt: dict[str, object]) -> str:
    """Atomically replace the lane-wide headroom receipt and return its JSON."""
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
    except Exception as exc:  # The guard still fails closed if receipt storage is unavailable.
        print(f"gig_disk_guard: could not persist receipt: {exc}", file=sys.stderr)
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


def _host_state_dir() -> Path:
    """Return the host control state without relying on a shell wrapper."""
    configured = (
        os.environ.get("GIG_HOST_STATE_DIR")
        or os.environ.get("DISK_CONTROL_STATE_DIR")
        or os.environ.get("OPENCLAW_STATE_DIR")
        or os.environ.get("LIFE_MANAGER_HOST_STATE_DIR")
    )
    if configured:
        return Path(configured).expanduser()
    # The production sentinel and emergency guard both write here. Installers can
    # override it above when Life Manager is used without OpenClaw.
    return Path.home() / ".openclaw" / "state"


def _producer_gate() -> tuple[str, Path] | None:
    """Read the shared Life Manager stop contract before starting a producer."""
    host_state = _host_state_dir()
    try:
        if not host_state.is_dir():
            return "disk_policy_unavailable", host_state
        # Validate that the control directory is readable before treating missing
        # flags as a safe state.
        next(iter(host_state.iterdir()), None)
    except OSError:
        return "disk_policy_unavailable", host_state
    for filename, reason in _POLICY_FLAGS:
        if (
            filename == "disk-writers.stop"
            and os.environ.get("GIG_IGNORE_DISK_WRITERS_STOP", "")
            .strip()
            .lower()
            in {"1", "true", "yes"}
        ):
            continue
        if (
            filename == "disk-pressure.block"
            and os.environ.get("GIG_IGNORE_DISK_PRESSURE_BLOCK", "")
            .strip()
            .lower()
            in {"1", "true", "yes"}
        ):
            continue
        flag = host_state / filename
        try:
            entry = flag.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            # A control path that cannot be read is not proof that the host is safe.
            return "disk_policy_unavailable", flag
        if not stat.S_ISREG(entry.st_mode):
            return "disk_policy_unavailable", flag
        return reason, flag
    return None


def disk_headroom_ok() -> bool:
    """Return whether this producer may allocate new work on the host."""
    gate = _producer_gate()
    if gate is not None:
        reason, flag = gate
        try:
            available_bytes = int(shutil.disk_usage(_state_dir()).free)
        except Exception:
            available_bytes = None
        _failure(
            reason,
            available_bytes,
            metadata={
                "gate": _PRODUCER_GATE,
                "flag_path": str(flag),
            },
        )
        return False
    try:
        available_bytes = int(shutil.disk_usage(_state_dir()).free)
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
        print("gig_disk_guard: missing child argv", file=sys.stderr)
        return 2

    if not disk_headroom_ok():
        return 1

    os.execvpe(remaining[0], remaining, os.environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
