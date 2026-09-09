#!/usr/bin/env python3
"""CEO allocation overlay over the repository-owned loop registry."""
from __future__ import annotations

import json
import fcntl
from copy import deepcopy
from pathlib import Path

try:
    from .registry_write_gate import atomic_write_registry
except ImportError:  # Executed with lib/ directly on sys.path by repository CLIs.
    from registry_write_gate import atomic_write_registry


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def read_overrides(state_root: Path | str) -> dict:
    path = Path(state_root) / "config" / "allocation-overrides.json"
    try:
        value = _read_object(path)
    except FileNotFoundError:
        return {}
    overrides = value.get("allocations")
    if not isinstance(overrides, dict):
        raise ValueError(f"{path} has no allocations object")
    return overrides


def effective_registry(config_root: Path | str, state_root: Path | str) -> dict:
    registry = deepcopy(_read_object(Path(config_root) / "config" / "loop-registry.json"))
    loops = registry.get("loops")
    if not isinstance(loops, dict):
        raise ValueError("repository registry has no loops object")
    for loop_id, allocation in read_overrides(state_root).items():
        entry = loops.get(loop_id)
        if isinstance(entry, dict) and isinstance(allocation, dict):
            entry["allocation"] = deepcopy(allocation)
    return registry


def write_allocation(state_root: Path | str, loop_id: str, allocation: dict) -> None:
    config_dir = Path(state_root) / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    with (config_dir / ".allocation-overrides.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        overrides = read_overrides(state_root)
        overrides[loop_id] = deepcopy(allocation)
        atomic_write_registry(
            str(config_dir / "allocation-overrides.json"),
            {"version": 1, "allocations": overrides},
        )
