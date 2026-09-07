#!/usr/bin/env python3
"""Own a headed CloakBrowser context for the lifetime of the process."""

from __future__ import annotations

import argparse
import os
import pwd
import signal
import stat
import subprocess
import time
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARD = _REPO_ROOT / "runtime/host/disk_admission.py"
_READABLE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_REMOVED_ENV = (
    "LIFE_MANAGER_DISK_HEADROOM_KIB", "LIFE_MANAGER_HOST_STATE_DIR",
    "LIFE_MANAGER_PRODUCER_STATE_DIR", "LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK",
    "LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP",
    "GIG_DISK_HEADROOM_KIB", "GIG_HOST_STATE_DIR", "GIG_STATE_DIR",
    "GIG_IGNORE_DISK_PRESSURE_BLOCK", "GIG_IGNORE_DISK_WRITERS_STOP",
    "DISK_CONTROL_STATE_DIR", "OPENCLAW_STATE_DIR",
)


def _canonical_home() -> Path | None:
    try:
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return None
    return home if home.is_absolute() and home.is_dir() else None


def _disk_preflight(home: Path | None = None, guard: Path | None = None) -> bool:
    home = _canonical_home() if home is None else home
    if home is None or not home.is_absolute() or not home.is_dir():
        return False
    guard = _GUARD if guard is None else guard
    try:
        if (
            guard.is_symlink()
            or not guard.is_file()
            or not guard.stat().st_mode & _READABLE
        ):
            return False
        required_kib = int(os.environ.get("BROWSER_DISK_HEADROOM_KIB", "524288"))
        if not 262_144 <= required_kib <= 4_194_304:
            return False
        child_env = os.environ.copy()
        for key in _REMOVED_ENV:
            child_env.pop(key, None)
        child_env.update(
            {
                "HOME": str(home),
                "LIFE_MANAGER_DISK_HEADROOM_KIB": str(required_kib),
                "LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK": "1",
                "LIFE_MANAGER_HOST_STATE_DIR": str(home / ".local/state/life-manager/state"),
                "LIFE_MANAGER_PRODUCER_STATE_DIR": str(home / ".local/state/life-manager/browser-provision"),
            }
        )
        result = subprocess.run(
            ["/usr/bin/python3", "-I", str(guard), "/usr/bin/true"],
            env=child_env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(result, "returncode", 1) == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--renderer-limit", default="24")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        port = int(args.port)
        renderer_limit = int(args.renderer_limit)
    except (TypeError, ValueError):
        return 1
    if not 0 <= port <= 65_535 or not 1 <= renderer_limit <= 64 or not _disk_preflight():
        return 1
    if args.preflight_only:
        return 0
    from cloakbrowser import launch_persistent_context

    context = launch_persistent_context(
        args.profile,
        headless=False,
        humanize=True,
        args=[
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--disable-features=MacAppCodeSignClone",
            f"--renderer-process-limit={renderer_limit}",
            "--disk-cache-size=67108864",
            "--media-cache-size=33554432",
            f"--disk-cache-dir={_canonical_home() / '.cache' / 'life-manager-daily-driver'}",
        ],
    )
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"persistent context alive on 127.0.0.1:{port}", flush=True)
    try:
        while not stopping:
            time.sleep(1)
    finally:
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
