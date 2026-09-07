#!/usr/bin/env python3
"""Run the isolated Affiliate EN CloakBrowser owned by launchd."""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
import sys
import time
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUARD = _REPO_ROOT / "runtime/host/disk_admission.py"
_START_URLS = {
    "affiliate-browser": "https://elevenlabs.io/app/home",
    "affiliate-impact-browser": "https://app.impact.com/login.user",
    "affiliate-x-browser": "https://x.com/home",
}
_READABLE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_REMOVED_ENV = (
    "LIFE_MANAGER_DISK_HEADROOM_KIB", "LIFE_MANAGER_HOST_STATE_DIR",
    "LIFE_MANAGER_PRODUCER_STATE_DIR", "LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK",
    "LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP",
    "GIG_DISK_HEADROOM_KIB", "GIG_HOST_STATE_DIR", "GIG_STATE_DIR",
    "GIG_IGNORE_DISK_PRESSURE_BLOCK", "GIG_IGNORE_DISK_WRITERS_STOP",
    "DISK_CONTROL_STATE_DIR", "OPENCLAW_STATE_DIR",
)


def _exec_with_owner(port: int, profile: Path, owner: str) -> None:
    owner_script = Path(__file__).resolve().parents[3] / "runtime/host/browser_port_owner.py"
    if owner_script.is_symlink() or not owner_script.is_file() or not os.access(owner_script, os.R_OK):
        raise RuntimeError("browser port owner is missing or unsafe")
    environment = os.environ.copy()
    environment["AFFILIATE_BROWSER_PORT_OWNED"] = "1"
    argv = [
        "/usr/bin/python3", "-I", str(owner_script), "run",
        "--port", str(port), "--profile", str(profile), "--owner", owner,
        "--", sys.executable, str(Path(__file__).resolve()),
    ]
    os.execve(argv[0], argv, environment)


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
        child_env = os.environ.copy()
        for key in _REMOVED_ENV:
            child_env.pop(key, None)
        child_env.update(
            {
                "HOME": str(home),
                "LIFE_MANAGER_DISK_HEADROOM_KIB": "524288",
                "LIFE_MANAGER_HOST_STATE_DIR": str(home / ".local/state/life-manager/state"),
                "LIFE_MANAGER_PRODUCER_STATE_DIR": str(home / ".local/state/life-manager/affiliate"),
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


def _cdp_port() -> int | None:
    try:
        port = int(os.environ.get(
            "AFFILIATE_CDP_PORT",
            os.environ.get("LIFE_MANAGER_BROWSER_CDP_PORT", "9324"),
        ))
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65_535 else None


def _renderer_limit() -> int | None:
    try:
        limit = int(os.environ.get("AFFILIATE_BROWSER_RENDERER_LIMIT", "8"))
    except (TypeError, ValueError):
        return None
    return limit if 1 <= limit <= 64 else None


def _browser_profile() -> Path:
    return Path(os.environ.get(
        "AFFILIATE_BROWSER_PROFILE",
        os.environ.get("LIFE_MANAGER_BROWSER_PROFILE", "~/.cloak/profiles/affiliate/en"),
    )).expanduser()


def _start_url() -> str:
    return os.environ.get(
        "AFFILIATE_START_URL",
        _START_URLS.get(
            os.environ.get("LIFE_MANAGER_LOOP_ID", "affiliate-browser"),
            _START_URLS["affiliate-browser"],
        ),
    )


def main() -> int:
    if not _disk_preflight():
        return 1
    port = _cdp_port()
    renderer_limit = _renderer_limit()
    if port is None or renderer_limit is None:
        return 1
    profile = _browser_profile()
    if not profile.is_absolute():
        return 1
    if os.environ.get("AFFILIATE_BROWSER_PORT_OWNED") != "1":
        owner = os.environ.get("LIFE_MANAGER_LOOP_ID", "affiliate-browser")
        try:
            _exec_with_owner(port, profile, owner)
        except (OSError, RuntimeError):
            return 1
        return 1
    from cloakbrowser import launch_persistent_context

    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    context = launch_persistent_context(
        str(profile), headless=False,
        args=[f"--remote-debugging-port={port}", "--remote-allow-origins=*",
              "--disable-features=MacAppCodeSignClone",
              f"--renderer-process-limit={renderer_limit}"],
    )
    pages = context.pages
    page = pages[0] if pages else context.new_page()
    if page.url == "about:blank":
        page.goto(
            _start_url(),
            wait_until="domcontentloaded",
            timeout=30_000,
        )
    while True:
        time.sleep(60)
if __name__ == "__main__":
    raise SystemExit(main())
