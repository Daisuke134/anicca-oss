#!/usr/bin/env python3
"""Plan or install the remaining Gate 15 weekly owner-report LaunchAgent.

The installer deliberately owns only the ``marketing-owner-*`` labels.  Gate
16 is responsible for retiring the legacy aggregate reporters after shadow
evidence is complete; this module never edits or unloads those jobs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import plistlib
import re
import subprocess
import sys
import tempfile
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
DEFAULT_HOME = pathlib.Path.home()
DEFAULT_LAUNCH_DIR = DEFAULT_HOME / "Library" / "LaunchAgents"
PYTHON = sys.executable
CLI_RELATIVE = pathlib.Path("skills/earn/marketing-engine/report/owner_report_cli.py")
STATE_RELATIVE = pathlib.Path("skills/earn/marketing-engine/state")
LOG_RELATIVE = pathlib.Path("Library/Logs/anicca")

LABELS = (
    "ai.anicca.marketing-owner-weekly",
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _paths(repo_root: pathlib.Path, home: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    repo_root = pathlib.Path(repo_root)
    home = pathlib.Path(home)
    return repo_root / CLI_RELATIVE, repo_root / STATE_RELATIVE, home / LOG_RELATIVE


def _common_job(
    *,
    label: str,
    repo_root: pathlib.Path,
    home: pathlib.Path,
    log_dir: pathlib.Path,
) -> dict[str, Any]:
    return {
        "Label": label,
        "ProcessType": "Background",
        "RunAtLoad": False,
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {
            "HOME": str(home),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin",
        },
        "StandardOutPath": str(log_dir / f"{label}.out.log"),
        "StandardErrorPath": str(log_dir / f"{label}.err.log"),
    }


def _sweep_args(
    *,
    python: str,
    cli: pathlib.Path,
    state_root: pathlib.Path,
    kind: str,
) -> list[str]:
    return [
        python,
        str(cli),
        "sweep",
        "--kind",
        kind,
        "--state-root",
        str(state_root),
    ]


def build_plists(repo_root: pathlib.Path, home: pathlib.Path) -> dict[str, bytes]:
    """Return the remaining Gate 15 weekly LaunchAgent plist payload.

    ``repo_root`` is explicit so callers can inspect a canonical checkout
    without accidentally embedding this disposable worktree.  This function
    is pure: it only renders bytes and does not create directories or write
    files.
    """

    repo_root = pathlib.Path(repo_root)
    home = pathlib.Path(home)
    cli, state_root, log_dir = _paths(repo_root, home)

    weekly = _common_job(
        label=LABELS[0], repo_root=repo_root, home=home, log_dir=log_dir
    )
    # launchd uses Sunday == 0 for StartCalendarInterval.  No timezone is
    # embedded; calendar values therefore use the host's local timezone.
    weekly["StartCalendarInterval"] = {"Weekday": 0, "Hour": 21, "Minute": 0}
    weekly["ProgramArguments"] = _sweep_args(
        python=PYTHON, cli=cli, state_root=state_root, kind="portfolio_weekly"
    )

    jobs = (weekly,)
    return {
        job["Label"]: plistlib.dumps(job, fmt=plistlib.FMT_XML, sort_keys=True)
        for job in jobs
    }


def _status(path: pathlib.Path, payload: bytes) -> tuple[str, bool, bytes | None]:
    """Return plan status and current bytes without mutating ``path``."""

    try:
        current = path.read_bytes()
    except FileNotFoundError:
        return "create", True, None
    if current == payload:
        return "no-change", False, current
    return "update", True, current


def plan(
    repo_root: pathlib.Path = REPO_ROOT,
    home: pathlib.Path = DEFAULT_HOME,
    launch_dir: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """Describe create/update/no-change status for the remaining target file."""

    launch_dir = pathlib.Path(launch_dir) if launch_dir is not None else pathlib.Path(home) / "Library" / "LaunchAgents"
    payloads = build_plists(repo_root, home)
    rows: list[dict[str, Any]] = []
    for label in LABELS:
        path = launch_dir / f"{label}.plist"
        payload = payloads[label]
        status, changed, current = _status(path, payload)
        rows.append({
            "label": label,
            "path": str(path),
            "status": status,
            "exists": current is not None,
            "changed": changed,
            "current_sha256": _sha256(current) if current is not None else None,
            "target_sha256": _sha256(payload),
        })
    return rows


def _atomic_write(path: pathlib.Path, payload: bytes) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_launchctl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _field_matches(text: str, names: tuple[str, ...], value: object) -> bool:
    """Return whether one exact field/value pair occurs in ``text``.

    A bare ``field in text`` plus ``value in text`` check is unsafe: a stale
    value elsewhere in ``launchctl print`` could mask a changed schedule.  The
    regular expression binds the value to the same assignment or mapping
    member as the field, with numeric boundaries that reject prefixes such as
    ``120`` for an expected ``12``.
    """

    expected = str(value).lower()
    for name in names:
        pattern = re.compile(
            rf"(?<![a-z0-9_])['\"]?{re.escape(name.lower())}['\"]?"
            rf"\s*(?:=>|=|:)\s*['\"]?{re.escape(expected)}['\"]?(?![a-z0-9_.-])",
            re.IGNORECASE,
        )
        if pattern.search(text):
            return True
    return False


def _balanced_block(text: str, start: int) -> str:
    """Return one brace-delimited block beginning at or after ``start``."""

    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "{":
        return ""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def _calendar_descriptors(text: str) -> list[str]:
    """Extract schedule descriptor blocks from real or synthetic readback."""

    # ``launchctl print`` uses ``event triggers = { ... descriptor = { ... } }``.
    triggers = re.search(r"event\s+triggers\s*(?:=|:)", text, re.IGNORECASE)
    if triggers is not None:
        event_block = _balanced_block(text, triggers.end())
        descriptors: list[str] = []
        for match in re.finditer(r"descriptor\s*(?:=|:)", event_block, re.IGNORECASE):
            descriptor = _balanced_block(event_block, match.end())
            if descriptor:
                descriptors.append(descriptor)
        if descriptors:
            return descriptors

    # Keep compatibility with the earlier synthetic fixture shape while the
    # live path uses event triggers above.
    legacy = re.search(r"start\s*calendar\s*interval\s*(?:=|:)", text, re.IGNORECASE)
    if legacy is None:
        return []
    descriptor = _balanced_block(text, legacy.end())
    return [descriptor] if descriptor else [text[legacy.start() :]]


def _readback_matches(output: str, payload: bytes, label: str) -> bool:
    """Validate the loaded definition represented by ``launchctl print``.

    ``launchctl print`` is human-readable rather than plist XML, so matching
    is deliberately tolerant of ``key = value`` versus ``key: value`` and
    whitespace while remaining strict about the owned label, canonical
    working/program paths, owner-report CLI, and schedule fields.
    """

    if not isinstance(output, str) or not output.strip():
        return False
    try:
        expected = plistlib.loads(payload)
    except plistlib.InvalidFileException:
        return False
    text = output.lower()
    if str(expected.get("Label", "")).lower() not in text:
        return False
    if str(expected.get("WorkingDirectory", "")).lower() not in text:
        return False
    arguments = expected.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments:
        return False
    owned_entrypoint = next(
        (
            str(argument)
            for argument in arguments
            if any(
                name in str(argument)
                for name in ("owner_report_cli.py", "truth_pipeline.py")
            )
        ),
        None,
    )
    if owned_entrypoint is None or owned_entrypoint.lower() not in text:
        return False
    if label.lower() != str(expected.get("Label", "")).lower():
        return False

    if "StartInterval" in expected:
        if not _field_matches(
            text,
            ("startinterval", "start interval", "run interval"),
            expected["StartInterval"],
        ):
            return False
    else:
        calendar = expected.get("StartCalendarInterval")
        if not isinstance(calendar, dict):
            return False
        descriptors = _calendar_descriptors(text)
        # These owner-report jobs each have one calendar descriptor.  Multiple
        # descriptors are ambiguous: accepting any one could borrow fields
        # from an unrelated event block, so fail closed instead.
        if len(descriptors) != 1:
            return False
        descriptor = descriptors[0]
        if not all(_field_matches(descriptor, (key,), value) for key, value in calendar.items()):
            return False
    return True


def _reload_and_readback(path: pathlib.Path, label: str, payload: bytes) -> dict[str, Any]:
    """Reload one owned label and prove launchd can print its definition."""

    domain = f"gui/{os.getuid()}"
    # Bootout is deliberately scoped to the one new owner-report label.  A
    # missing job is expected on first install and is not an error.
    bootout = _run_launchctl(["bootout", f"{domain}/{label}"])
    bootstrap = _run_launchctl(["bootstrap", domain, str(path)])
    if bootstrap.returncode != 0:
        detail = (bootstrap.stderr or bootstrap.stdout or "").strip()
        raise RuntimeError(f"bootstrap failed for {label}: {detail}")
    kickstart = _run_launchctl(["kickstart", "-k", f"{domain}/{label}"])
    if kickstart.returncode != 0:
        detail = (kickstart.stderr or kickstart.stdout or "").strip()
        raise RuntimeError(f"kickstart failed for {label}: {detail}")
    readback = _run_launchctl(["print", f"{domain}/{label}"])
    if readback.returncode != 0:
        detail = (readback.stderr or readback.stdout or "").strip()
        raise RuntimeError(f"readback failed for {label}: {detail}")
    output = readback.stdout or ""
    if not _readback_matches(output, payload, label):
        raise RuntimeError(f"readback mismatch for {label}")
    return {
        "bootout_returncode": bootout.returncode,
        "bootstrap_returncode": bootstrap.returncode,
        "kickstart_returncode": kickstart.returncode,
        "loaded_readback": True,
        "readback_mentions_cli": True,
    }


def apply(
    repo_root: pathlib.Path = REPO_ROOT,
    home: pathlib.Path = DEFAULT_HOME,
    launch_dir: pathlib.Path | None = None,
) -> list[dict[str, Any]]:
    """Atomically install and read back only the remaining owned label."""

    repo_root = pathlib.Path(repo_root)
    home = pathlib.Path(home)
    launch_dir = pathlib.Path(launch_dir) if launch_dir is not None else home / "Library" / "LaunchAgents"
    payloads = build_plists(repo_root, home)
    # Ensure launchd's stdout/stderr destinations are writable without touching
    # any legacy scheduler paths.  Plan mode never calls this function.
    _cli, _state_root, log_dir = _paths(repo_root, home)
    log_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for label in LABELS:
        path = launch_dir / f"{label}.plist"
        payload = payloads[label]
        status, changed, current = _status(path, payload)
        if changed:
            _atomic_write(path, payload)
        row: dict[str, Any] = {
            "label": label,
            "path": str(path),
            "status": status,
            "changed": changed,
            "current_sha256": _sha256(current) if current is not None else None,
            "installed_sha256": _sha256(payload),
        }
        row.update(_reload_and_readback(path, label, payload))
        rows.append(row)
    return rows


def _write_output(path: pathlib.Path | None, rendered: str) -> None:
    if path is None:
        return
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="report changes without writing")
    mode.add_argument("--apply", action="store_true", help="atomically install and reload owned jobs")
    parser.add_argument("--repo-root", type=pathlib.Path, default=REPO_ROOT)
    parser.add_argument("--home", type=pathlib.Path, default=DEFAULT_HOME)
    parser.add_argument("--launch-dir", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args(argv)
    launch_dir = args.launch_dir
    if launch_dir is None:
        launch_dir = args.home / "Library" / "LaunchAgents"

    try:
        if args.plan:
            result: dict[str, Any] = {
                "action": "plan",
                "repo_root": str(args.repo_root),
                "launch_dir": str(launch_dir),
                "rows": plan(args.repo_root, args.home, launch_dir),
            }
        else:
            result = {
                "action": "apply",
                "repo_root": str(args.repo_root),
                "launch_dir": str(launch_dir),
                "rows": apply(args.repo_root, args.home, launch_dir),
            }
    except (OSError, ValueError, RuntimeError, plistlib.InvalidFileException) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    # A plan is a read-only operation, including when --output is supplied.
    # Only apply may persist an optional machine-readable result.
    if args.apply:
        _write_output(args.output, rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
