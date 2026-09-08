#!/usr/bin/env python3
"""Publish one persisted Writer note target under the fixed ¥500 contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


import sys as _pii_sys  # noqa: E402 -- fail-closed PII gate wiring
from pathlib import Path as _PiiPath  # noqa: E402
_pii_sys.path.insert(0, str(next(
    _p / "_shared"
    for _p in _PiiPath(__file__).resolve().parents
    if (_p / "_shared" / "pii_gate.py").is_file()
)))
from pii_gate import gate_files, gate_run_dir  # noqa: E402,F401

HERE = Path(__file__).resolve().parent
NOTE = HERE / "note-publish"
HOME = Path.home()


def adapt_note_eyecatch(source: Path, destination: Path) -> None:
    """Create note's required 1280:670 center crop from immutable media."""
    from PIL import Image

    with Image.open(source) as image:
        image = image.convert("RGB")
        width, height = image.size
        target_ratio = 1280 / 670
        source_ratio = width / height
        if source_ratio > target_ratio:
            crop_width = round(height * target_ratio)
            left = (width - crop_width) // 2
            box = (left, 0, left + crop_width, height)
        else:
            crop_height = round(width / target_ratio)
            top = (height - crop_height) // 2
            box = (0, top, width, top + crop_height)
        adapted = image.crop(box).resize(
            (1280, 670), Image.Resampling.LANCZOS
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        adapted.save(destination, format="PNG")


def command(name: str, default: list[str]) -> list[str]:
    override = os.environ.get(name)
    return shlex.split(override) if override else default


def run(argv: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        argv,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def main() -> int:
    state_path = Path(os.environ.get("ARTICLE_PUBLICATION_STATE", ""))
    state = json.loads(state_path.read_text())
    pair = state.get("pairs", {}).get("note/ja", {})
    target = str(pair.get("target", ""))
    if (
        pair.get("status") != "intent"
        or pair.get("target_kind") != "note-key"
        or not target
    ):
        raise SystemExit("refuse managed note publish: durable intent is missing")

    headline = state.get("media", {}).get("headline_image", {})
    headline_path = Path(str(headline.get("path", "")))
    if (
        not headline_path.is_file()
        or hashlib.sha256(headline_path.read_bytes()).hexdigest()
        != headline.get("sha256")
    ):
        raise SystemExit("refuse managed note publish: immutable eyecatch changed")
    work = HOME / ".cloak/note-work"
    work.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(headline_path, work / "thumb.png")

    policy_argv = command(
        "NOTE_POLICY_COMMAND",
        ["python3", str(NOTE / "note_monetization_policy.py")],
    )
    policy = json.loads(run([*policy_argv, "desired-state"]))
    if policy != {
        "access_model": "one_time_purchase",
        "currency": "JPY",
        "paywall_required": True,
        "price_minor": 500,
        "publisher_args": ["--price", "500"],
    }:
        raise SystemExit("refuse managed note publish: money policy drifted")

    cloak_python = os.environ.get("WRITER_BROWSER_PYTHON", sys.executable)
    if os.environ.get("NOTE_EYECATCH_COMMAND"):
        eyecatch_argv = command("NOTE_EYECATCH_COMMAND", [])
    else:
        note_mcp = Path(
            os.environ.get(
                "NOTE_MCP_DIR", str(HOME / ".openclaw/external/note-mcp")
            )
        )
        run(["bash", str(HERE / "ensure-note-mcp-runtime.sh"), str(note_mcp)])
        adapted_eyecatch = work / "note-eyecatch-1280x670.png"
        adapt_note_eyecatch(headline_path, adapted_eyecatch)
        eyecatch_argv = [
            str(note_mcp / ".venv/bin/python"),
            str(NOTE / "set-eyecatch-api.py"),
            str(adapted_eyecatch),
        ]
    eyecatch = run(
        eyecatch_argv,
        env={**os.environ, "NOTE_KEY": target},
    )
    if not re.search(
        r"EYECATCH_IN_EDITOR:\s+https://assets\.st-note\.com/\S+",
        eyecatch,
    ):
        raise SystemExit("refuse managed note publish: eyecatch readback failed")

    enable_argv = command(
        "NOTE_ENABLE_COMMAND",
        ["bash", str(NOTE / "publish-to-note.sh"), "enable-publish"],
    )
    disable_argv = command(
        "NOTE_DISABLE_COMMAND",
        ["bash", str(NOTE / "publish-to-note.sh"), "disable-publish"],
    )
    publisher_argv = command(
        "NOTE_PUBLISH_COMMAND",
        [cloak_python, str(NOTE / "publish-paid.py")],
    )
    # Fail-closed PII gate at the publish boundary: scan the frozen artifacts this target is
    # about to make public. An unset ARTICLE_RUN_DIR is itself a refusal.
    gate_run_dir("publish-note-managed", os.environ.get("ARTICLE_RUN_DIR", ""), pair="note/ja")
    run(enable_argv)
    try:
        output = run(
            [
                *publisher_argv,
                "--key",
                target,
                "--price",
                "500",
                "--after-chars",
                "2500",
                "--arm",
            ],
            env={**os.environ, "NOTE_MODE": "go"},
        )
    finally:
        run(disable_argv)
    if not re.search(
        rf"PAID_PUBLISHED key={re.escape(target)} price=500 verified=true",
        output,
    ):
        raise SystemExit("managed note publisher lacked paid API proof")
    updated = json.loads(state_path.read_text())
    if updated.get("pairs", {}).get("note/ja", {}).get("status") != "live":
        raise SystemExit("managed note publisher lacked durable live receipt")
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
