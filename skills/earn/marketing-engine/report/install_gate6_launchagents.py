#!/usr/bin/env python3
"""Reversibly route existing marketing LaunchAgents through Gate 6 reporting."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import plistlib
import subprocess
import sys
import tempfile


HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
SCHEDULED_RUNNER = HERE / "scheduled_runner.py"
DEFAULT_LAUNCH_DIR = pathlib.Path.home() / "Library" / "LaunchAgents"
MAPPINGS = {
    "ai.anicca.marketing-score-daily": "score",
    "ai.anicca.marketing-metrics-daily": "metrics",
    "ai.anicca.marketing-dashboard": "dashboard",
    "ai.anicca.self-improve-evolve": "self-improve",
    "ai.anicca.capafy-ig-marketing-daily": "capafy",
}


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def rewrite_plist(source: dict, runner_id: str) -> dict:
    rewritten = dict(source)
    rewritten["ProgramArguments"] = [sys.executable, str(SCHEDULED_RUNNER), runner_id]
    rewritten["WorkingDirectory"] = str(REPO_ROOT)
    return rewritten


def apply_one(path: pathlib.Path, runner_id: str,
              backup_dir: pathlib.Path) -> dict:
    path = pathlib.Path(path)
    original_bytes = path.read_bytes()
    original = plistlib.loads(original_bytes)
    if original.get("Label") not in MAPPINGS:
        raise ValueError(f"unexpected LaunchAgent label in {path}")
    if MAPPINGS[original["Label"]] != runner_id:
        raise ValueError(f"runner mismatch for {original['Label']}")
    target_bytes = plistlib.dumps(rewrite_plist(original, runner_id), sort_keys=True)
    backup_dir = pathlib.Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{original['Label']}.original.plist"
    if not backup_path.exists():
        backup_path.write_bytes(original_bytes)
    changed = original_bytes != target_bytes
    if changed:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(target_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return {
        "label": original["Label"],
        "runner_id": runner_id,
        "path": str(path),
        "backup_path": str(backup_path),
        "original_sha256": _hash(original_bytes),
        "installed_sha256": _hash(target_bytes),
        "changed": changed,
    }


def plan(launch_dir: pathlib.Path) -> list[dict]:
    rows = []
    for label, runner_id in MAPPINGS.items():
        path = pathlib.Path(launch_dir) / f"{label}.plist"
        payload = path.read_bytes()
        source = plistlib.loads(payload)
        target = plistlib.dumps(rewrite_plist(source, runner_id), sort_keys=True)
        rows.append({
            "label": label,
            "runner_id": runner_id,
            "path": str(path),
            "exists": path.exists(),
            "current_sha256": _hash(payload),
            "target_sha256": _hash(target),
            "would_change": payload != target,
        })
    return rows


def _reload(row: dict) -> dict:
    domain = f"gui/{os.getuid()}"
    label = row["label"]
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    loaded = subprocess.run(
        ["launchctl", "bootstrap", domain, row["path"]],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if loaded.returncode != 0:
        pathlib.Path(row["path"]).write_bytes(pathlib.Path(row["backup_path"]).read_bytes())
        rollback = subprocess.run(
            ["launchctl", "bootstrap", domain, row["path"]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        raise RuntimeError(
            f"bootstrap failed for {label}: {loaded.stderr.strip()}; "
            f"original restored, rollback bootstrap rc={rollback.returncode}")
    readback = subprocess.run(
        ["launchctl", "print", f"{domain}/{label}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    return {
        "bootstrap_returncode": loaded.returncode,
        "loaded_readback": readback.returncode == 0,
        "readback_mentions_runner": "scheduled_runner.py" in readback.stdout,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--launch-dir", type=pathlib.Path, default=DEFAULT_LAUNCH_DIR)
    parser.add_argument("--backup-dir", type=pathlib.Path,
                        default=HERE.parent / "evidence" / "schedulers" / "gate6-backups")
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            result = {"action": "plan", "rows": plan(args.launch_dir)}
        else:
            rows = []
            for label, runner_id in MAPPINGS.items():
                row = apply_one(args.launch_dir / f"{label}.plist", runner_id,
                                args.backup_dir)
                row.update(_reload(row))
                rows.append(row)
            result = {"action": "apply", "rows": rows}
    except (OSError, ValueError, RuntimeError, plistlib.InvalidFileException) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
