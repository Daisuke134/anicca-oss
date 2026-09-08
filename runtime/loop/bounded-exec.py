#!/usr/bin/env python3
"""Shared portable wall-clock bound that terminates the child process group."""

from __future__ import annotations

from contextlib import suppress
import os
import signal
import subprocess
import sys
import time

STOP_PATHS_ENV = "BOUNDED_EXEC_STOP_PATHS"
POLL_INTERVAL_SECONDS, DRAIN_GRACE_SECONDS = 0.1, 1.0


def _stop_requested() -> bool:
    for path in filter(None, os.environ.get(STOP_PATHS_ENV, "").split(os.pathsep)):
        try:
            return bool(os.lstat(path))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return True
        return True
    return False


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _terminate_group(process: subprocess.Popen[object]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + DRAIN_GRACE_SECONDS
    while _group_exists(process.pid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))
    if _group_exists(process.pid):
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: bounded-exec.py <seconds> <command> [args...]", file=sys.stderr)
        return 2
    try:
        timeout = float(sys.argv[1])
    except ValueError:
        print("bounded-exec.py: seconds must be numeric", file=sys.stderr)
        return 2
    if timeout <= 0:
        print("bounded-exec.py: seconds must be positive", file=sys.stderr)
        return 2

    received_signal = [0]

    def handle_signal(signum: int, _frame: object) -> None:
        received_signal[0] = signum

    previous_sigterm = signal.signal(signal.SIGTERM, handle_signal)
    previous_sigint = signal.signal(signal.SIGINT, handle_signal)
    try:
        if received_signal[0] or _stop_requested():
            return 128 + received_signal[0] if received_signal[0] else 143
        process = subprocess.Popen(sys.argv[2:], start_new_session=True)
        deadline = time.monotonic() + timeout
        while True:
            if process.poll() is not None:
                return 128 + received_signal[0] if received_signal[0] else process.wait()
            if received_signal[0] or _stop_requested():
                _terminate_group(process)
                return 128 + received_signal[0] if received_signal[0] else 143
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_group(process)
                return 124
            try:
                return 128 + received_signal[0] if received_signal[0] else process.wait(
                    timeout=min(POLL_INTERVAL_SECONDS, remaining)
                )
            except subprocess.TimeoutExpired:
                continue
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)


if __name__ == "__main__":
    raise SystemExit(main())
