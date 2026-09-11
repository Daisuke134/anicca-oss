#!/usr/bin/env python3
"""Canonical HeyGen Avatar IV renderer for the English ebook product."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable


IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
Executor = Callable[..., subprocess.CompletedProcess[str]]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def configuration(environment: dict[str, str] | None = None) -> dict:
    env = os.environ if environment is None else environment
    avatar_id = str(env.get("LM_EBOOK_EN_HEYGEN_AVATAR_ID", "")).strip()
    voice_id = str(env.get("LM_EBOOK_EN_HEYGEN_VOICE_ID", "")).strip()
    cli = str(env.get("LIFE_MANAGER_HEYGEN", "")).strip() or shutil.which("heygen")
    missing = []
    if not avatar_id:
        missing.append("LM_EBOOK_EN_HEYGEN_AVATAR_ID")
    if not voice_id:
        missing.append("LM_EBOOK_EN_HEYGEN_VOICE_ID")
    if not cli or not Path(cli).is_absolute() or not Path(cli).is_file() or not os.access(cli, os.X_OK):
        missing.append("heygen_cli")
    if missing:
        return {"status": "setup_required", "missing": missing}
    require(IDENTIFIER.fullmatch(avatar_id) is not None, "HeyGen avatar ID invalid")
    require(IDENTIFIER.fullmatch(voice_id) is not None, "HeyGen voice ID invalid")
    return {"status": "ready", "cli": cli, "avatar_id": avatar_id, "voice_id": voice_id}


def build_request(script: str, config: dict) -> dict:
    require(config.get("status") == "ready", "HeyGen setup is incomplete")
    require(isinstance(script, str) and script.strip(), "HeyGen script is required")
    require(len(script) <= 5000, "HeyGen script is too long")
    return {
        "type": "avatar",
        "avatar_id": config["avatar_id"],
        "script": script.strip(),
        "voice_id": config["voice_id"],
        "voice_settings": {"speed": 0.9, "locale": "en-US"},
        "engine": {"type": "avatar_iv"},
        "aspect_ratio": "9:16",
        "resolution": "1080p",
        "output_format": "mp4",
    }


def _run(executor: Executor, args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return executor(args, input=input_text, text=True, capture_output=True, check=True)


def render(
    *,
    script: str,
    output: Path,
    environment: dict[str, str] | None = None,
    executor: Executor = subprocess.run,
) -> dict:
    config = configuration(environment)
    if config["status"] != "ready":
        return {
            "renderer_id": "heygen-avatar-iv",
            "state": "setup_required",
            "missing": config["missing"],
            "external_effects": [],
        }
    output = Path(output)
    require(not output.exists(), "HeyGen render output already exists")
    staging = output.with_name(f".{output.name}.part")
    require(not staging.exists(), "HeyGen render staging output already exists")
    try:
        _run(executor, [config["cli"], "auth", "status"])
    except subprocess.CalledProcessError:
        return {
            "renderer_id": "heygen-avatar-iv",
            "state": "setup_required",
            "missing": ["heygen_auth"],
            "external_effects": [],
        }
    request = build_request(script, config)
    created = _run(
        executor,
        [config["cli"], "video", "create", "-d", "-", "--wait"],
        input_text=json.dumps(request, ensure_ascii=False),
    )
    response = json.loads(created.stdout)
    data = response.get("data", response)
    video_id = data.get("video_id")
    require(IDENTIFIER.fullmatch(str(video_id or "")) is not None, "HeyGen create receipt has no video ID")
    require(data.get("status") == "completed", "HeyGen video did not complete")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run(
            executor,
            [config["cli"], "video", "download", video_id, "--output-path", str(staging)],
        )
        require(staging.is_file() and staging.stat().st_size > 0, "HeyGen download produced no video")
        staging.replace(output)
    finally:
        if staging.exists():
            staging.unlink()
    return {
        "renderer_id": "heygen-avatar-iv",
        "state": "rendered",
        "video_id": video_id,
        "output": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "external_effects": ["heygen_video_created", "heygen_video_downloaded"],
    }
