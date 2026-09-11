#!/usr/bin/env python3
"""Central owner for shared immutable Life Manager release garbage collection."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.loop.loop_cleanup import gc_releases


def host_cleanup_command(root: Path, home: Path, state_dir=None) -> list[str]:
    state_dir = state_dir or home / ".local/state/life-manager/life-manager-disk-cleanup"
    return [sys.executable, str(root / "skills/self/disk-cleanup/disk_cleanup.py"),
            "--home", str(home), "--state-dir",
            str(state_dir)]


def host_cleanup_ok(returncode: int, result: object) -> bool:
    return (returncode == 0 and isinstance(result, dict)
            and result.get("errors") == 0 and result.get("protected_deletions") == 0)


def loaded_release_roots(agents_dir: Path, releases_root: Path) -> set[Path]:
    protected = set()
    base = releases_root.resolve()
    for plist_path in agents_dir.glob("ai.anicca.*.plist"):
        try:
            with plist_path.open("rb") as handle:
                plist = plistlib.load(handle)
        except Exception:
            continue
        for value in map(str, plist.get("ProgramArguments") or []):
            candidate = Path(os.path.expanduser(value))
            try:
                resolved = candidate.resolve(strict=True)
                relative = resolved.relative_to(base)
            except (OSError, ValueError):
                continue
            if relative.parts:
                release = base / relative.parts[0]
                if release.is_dir(): protected.add(release.resolve())
    return protected


def open_release_roots(releases_root: Path) -> set[Path]:
    """Return release roots named by a running process command."""
    completed = subprocess.run(
        ["ps", "-axo", "command="],
        capture_output=True, text=True, timeout=120,
    )
    if completed.returncode != 0:
        raise OSError(f"release process inventory failed: {completed.returncode}")
    requested_base = releases_root.expanduser()
    base = requested_base.resolve()
    commands = completed.stdout
    return {
        release.resolve()
        for release in base.iterdir()
        if release.is_dir() and any(
            candidate in commands
            for candidate in (str(release.resolve()), str(requested_base / release.name))
        )
    }


def release_gc(releases: Path, current: Path, agents: Path, keep: int) -> dict:
    """Collect releases while pinning every generation referenced by launchd."""
    protected = loaded_release_roots(agents, releases) | open_release_roots(releases)
    protected_file = Path(os.environ.get(
        "LIFE_MANAGER_PROTECTED_RELEASES", "~/.local/state/life-manager/protected-releases.json")).expanduser()
    try:
        values = json.loads(protected_file.read_text())
        if isinstance(values, list):
            protected.update(Path(value).expanduser().resolve() for value in values if isinstance(value, str))
    except (OSError, json.JSONDecodeError):
        pass
    result = gc_releases(releases, current, keep=keep, protected=protected)
    result["protected_release_count"] = len(protected)
    return result


def main() -> int:
    home = Path.home()
    loops_root = Path(os.environ.get("LOOPS_ROOT", "~/loops")).expanduser()
    releases = loops_root / "releases"
    current = loops_root / "current"
    agents = Path(os.environ.get(
        "LIFE_MANAGER_LAUNCH_AGENTS_DIR", "~/Library/LaunchAgents")).expanduser()
    if sys.argv[1:] == ["--release-gc-only"]:
        try:
            result = release_gc(releases, current, agents,
                                keep=int(os.environ.get("LIFE_MANAGER_RELEASE_KEEP", "1")))
        except (OSError, ValueError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True)); return 1
        result["ok"] = result["errors"] == 0
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0 if result["ok"] else 1
    try:
        cleanup_state = Path(os.environ.get(
            "LIFE_MANAGER_STATE_ROOT",
            home / ".local/state/life-manager/life-manager-disk-cleanup",
        )).expanduser()
        host_process = subprocess.run(
            host_cleanup_command(ROOT, home, cleanup_state),
            capture_output=True, text=True, timeout=240,
        )
        host_result = json.loads(host_process.stdout.splitlines()[-1]) if host_process.stdout.strip() else {}
        host_ok = host_cleanup_ok(host_process.returncode, host_result)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, IndexError) as error:
        host_ok, host_result = False, {"error": str(error)}
    try:
        result = release_gc(releases, current, agents,
                            keep=int(os.environ.get("LIFE_MANAGER_RELEASE_KEEP", "1")))
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True)); return 1
    result.update({"ok": result["errors"] == 0 and host_ok,
                   "host_cleanup": host_result,
                   "idle_reconcile": [],
                   "shared_cache_candidates": 0, "orphan_candidates": 0})
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
