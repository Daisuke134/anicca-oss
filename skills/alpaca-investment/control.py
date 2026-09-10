"""Durable, fail-closed owner control state for the investment loop."""

from __future__ import annotations

import json
import os
import tempfile
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIONS = frozenset({"pause", "resume", "kill"})


def read_control(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"paused": False, "killed": False}
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("investment_control_state_invalid") from error
    if not isinstance(value, dict) or set(value) - {
        "paused", "killed", "revision", "updated_at", "last_action",
    }:
        raise ValueError("investment_control_state_invalid")
    if not isinstance(value.get("paused"), bool) or not isinstance(value.get("killed"), bool):
        raise ValueError("investment_control_state_invalid")
    if isinstance(value.get("revision"), bool) or not isinstance(value.get("revision"), int) \
            or value["revision"] < 1:
        raise ValueError("investment_control_state_invalid")
    if value["killed"] and not value["paused"]:
        raise ValueError("investment_control_state_invalid")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
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


@contextmanager
def control_fence(root: Path):
    """Hold the control lock from the final state read through broker submit."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_descriptor = os.open(root / ".control.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_descriptor, 0o600)
        # Effecting wakes must serialize with each other as well as pause/kill.
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        yield read_control(root / "control.json")
    finally:
        os.close(lock_descriptor)


def apply_control(root: Path, action: str, now: str | None = None) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError("investment_control_action_invalid")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_descriptor = os.open(root / ".control.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        before = read_control(root / "control.json")
        if before["killed"]:
            return {"changed": False, "state": before}
        desired = {
            "pause": {"paused": True, "killed": False},
            "resume": {"paused": False, "killed": False},
            "kill": {"paused": True, "killed": True},
        }[action]
        if before["paused"] == desired["paused"] and before["killed"] == desired["killed"]:
            return {"changed": False, "state": before}
        observed = now or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        state = {**desired, "revision": before.get("revision", 0) + 1,
                 "updated_at": observed, "last_action": action}
        _atomic_json(root / "control.json", state)
        return {"changed": True, "state": state}
    finally:
        os.close(lock_descriptor)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--action", choices=sorted(ACTIONS), required=True)
    args = parser.parse_args()
    print(json.dumps(apply_control(args.state_root, args.action), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
