#!/usr/bin/env python3
"""Exec one command while holding an advisory file lock."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print(f"usage: {argv[0]} LOCK -- COMMAND [ARGS...]", file=sys.stderr)
        return 64
    path = Path(argv[1]).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    os.set_inheritable(descriptor, True)
    os.execvp(argv[3], argv[3:])
    return 70


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
