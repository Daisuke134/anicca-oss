#!/usr/bin/env python3
"""Per-loop cleanup boundary followed by exact immutable entrypoint exec."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from runtime.loop.loop_cleanup import cleanup_run_root
from runtime.loop.lm_loop import _apply_lock, _label_apply_lock_path
from runtime.loop.macos_loop_registry import validate_registry
from runtime.loop.runtime_event import append_runtime_event, build_runtime_event, build_runtime_start_event


def prepare_loop_run(registry: dict, loop_id: str, release_root: Path, *,
                     active_run_ids: set[str], now: float | None = None,
                     state_root: str | None = None,
                     log_root: str | None = None) -> tuple[list[str], dict]:
    validate_registry(registry)
    entry = registry["loops"].get(loop_id)
    if not isinstance(entry, dict):
        raise ValueError(f"unknown loop id: {loop_id}")
    executable = release_root.resolve() / entry["entrypoint"]
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"entrypoint missing or not executable: {entry['entrypoint']}")
    totals = {"evaluated_runs": 0, "removed_runs": 0, "reclaimed_bytes": 0,
              "preserved_runs": 0, "protected_deletions": 0, "errors": 0}
    seen = set()
    for value in (state_root or entry["state_root"], log_root or entry["log_root"]):
        root = Path(os.path.expanduser(value)).resolve()
        if root in seen:
            continue
        seen.add(root)
        result = cleanup_run_root(root, entry["cleanup"], active_run_ids, now=now)
        for key in totals:
            totals[key] += result[key]
    command = [str(executable)]
    if entry.get("adapter") == "python":
        command.insert(0, sys.executable)
    command.extend(entry.get("command", []))
    return command, totals


def reset_loop_scratch(state_root: Path, loop_id: str) -> Path:
    """Private scratch dir per loop, wiped every run so subprocess temp files cannot leak."""
    scratch = state_root / "loop-tmp" / loop_id
    if scratch.is_dir() and not scratch.is_symlink():
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    return scratch


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.chmod(name, 0o600); os.replace(name, path)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass


def _runtime_limit(entry: dict) -> int | None:
    if entry.get("cadence") == {"keep_alive": True}:
        return None
    return entry.get("runtime_timeout_seconds", 3600)


def _memory_admission_deferred(path: Path, started_ns: int) -> bool:
    try:
        if path.stat().st_mtime_ns < started_ns:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return value.get("status") == "deferred" and value.get("effect") == 0


def _terminal_outcome(return_code: int, *, memory_deferred: bool = False
                      ) -> tuple[bool, bool, str | None]:
    if return_code == 0:
        return True, False, None
    if return_code == 75 and memory_deferred:
        return False, True, "memory_admission_deferred"
    return False, False, f"entrypoint_exit_{return_code}"


def _run_entrypoint(command: list[str], env: dict[str, str] | None = None, *,
                    timeout_seconds: float | None = None,
                    termination_grace_seconds: float = 15) -> int:
    process = subprocess.Popen(command, start_new_session=True, env=env)
    previous = {}

    def forward(signum, _frame):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, forward)
    try:
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            forward(signal.SIGTERM, None)
            try:
                process.wait(timeout=termination_grace_seconds)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
            return 124
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return return_code if return_code >= 0 else 128 - return_code


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if len(args) != 2:
        print("usage: lm-loop-run <loop-id> <release-root>", file=sys.stderr); return 64
    loop_id, release_value = args
    release_root = Path(release_value).resolve()
    try:
        registry = json.loads((release_root / "config/loop-registry.json").read_text())
        manifest = json.loads((release_root / "RELEASE.json").read_text())
        if not isinstance(manifest.get("sha"), str) or len(manifest["sha"]) != 40:
            raise ValueError("invalid release manifest SHA")
        entry = registry.get("loops", {}).get(loop_id)
        if not isinstance(entry, dict):
            raise ValueError(f"unknown loop id: {loop_id}")
        loop_state_root = Path(os.path.expanduser(
            os.environ.get("LIFE_MANAGER_STATE_ROOT", entry["state_root"])))
        loop_log_root = os.path.expanduser(
            os.environ.get("LIFE_MANAGER_LOG_ROOT", entry["log_root"]))
        current = Path("~/loops/current").expanduser()
        item_lock = _label_apply_lock_path(current, entry["label"])
        with _apply_lock(current, item_lock):
            active = {value for value in os.environ.get("LIFE_MANAGER_ACTIVE_RUN_IDS", "").split(",") if value}
            command, cleanup = prepare_loop_run(
                registry, loop_id, release_root, active_run_ids=active, now=time.time(),
                state_root=str(loop_state_root), log_root=loop_log_root)
            receipt = loop_state_root / "cleanup-latest.json"
            _atomic_json(receipt, {"version": 1, "loop_id": loop_id,
                                  "release_sha": manifest["sha"], **cleanup})
            run_id = os.environ.get("LIFE_MANAGER_RUN_ID") or f"{time.time_ns():x}-{os.getpid()}"
            event_path = loop_state_root / "events.jsonl"
            try:
                append_runtime_event(event_path, build_runtime_start_event(
                    loop_id=loop_id, domain=entry["domain"], run_id=run_id,
                    release_sha=manifest["sha"], provider=entry["provider_route"],
                    profile_alias=None, effect_class=entry["effect_class"],
                ))
            except (OSError, ValueError) as error:
                print(f"lm-loop-run: start event failed: {error}", file=sys.stderr)
            scratch = reset_loop_scratch(loop_state_root, loop_id)
        try:
            memory_receipt = scratch / "memory-admission.json"
            started_ns = time.time_ns()
            return_code = _run_entrypoint(
                command,
                env={
                    **os.environ,
                    "LIFE_MANAGER_RELEASE_ROOT": str(release_root),
                    "TMPDIR": f"{scratch}/",
                    "NPM_CONFIG_CACHE": str(scratch / "npm-cache"),
                    "LIFE_MANAGER_MEMORY_RECEIPT": str(memory_receipt),
                },
                timeout_seconds=_runtime_limit(entry),
            )
            memory_deferred = _memory_admission_deferred(memory_receipt, started_ns)
        finally:
            # Scratch is never evidence. Every loop owns and removes its temporary
            # downloads, package caches, and build products when its pass ends.
            shutil.rmtree(scratch, ignore_errors=True)
        try:
            succeeded, deferred, blocker = _terminal_outcome(
                return_code, memory_deferred=memory_deferred)
            event = build_runtime_event(
                loop_id=loop_id, domain=entry["domain"], run_id=run_id,
                release_sha=manifest["sha"], provider=entry["provider_route"],
                profile_alias=None, effect_class=entry["effect_class"],
                succeeded=succeeded, deferred=deferred, blocker=blocker,
                evidence_scheme="lm-loop",
            )
            append_runtime_event(event_path, event)
        except (OSError, ValueError) as error:
            print(f"lm-loop-run: terminal event failed: {error}", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"lm-loop-run: {error}", file=sys.stderr); return 78
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
