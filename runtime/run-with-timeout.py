#!/usr/bin/env python3
"""Run one command with a portable timeout and process-group cleanup."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys


def _signal_group(process: subprocess.Popen, signal_number: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal_number)
    except (PermissionError, ProcessLookupError):
        if process.poll() is None:
            process.send_signal(signal_number)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grace-seconds", type=float, default=0)
    parser.add_argument("seconds", type=float)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.seconds <= 0 or args.grace_seconds < 0 or not args.command:
        parser.error("positive timeout, nonnegative grace, and command are required")

    process = subprocess.Popen(args.command, start_new_session=True)
    previous_handlers = {}
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signal_number] = signal.signal(
            signal_number,
            lambda received, _frame: _signal_group(process, received),
        )
    try:
        try:
            return_code = process.wait(timeout=args.seconds)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=args.grace_seconds)
            except subprocess.TimeoutExpired:
                _signal_group(process, signal.SIGKILL)
                process.wait()
            return 124
        return 128 - return_code if return_code < 0 else return_code
    finally:
        for signal_number, previous in previous_handlers.items():
            signal.signal(signal_number, previous)


if __name__ == "__main__":
    raise SystemExit(main())
