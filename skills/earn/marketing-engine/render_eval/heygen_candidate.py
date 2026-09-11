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
import tempfile
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


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def _receipt(output: Path, video_id: str) -> dict:
    return {
        "renderer_id": "heygen-avatar-iv",
        "state": "rendered",
        "video_id": video_id,
        "output": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "external_effects": ["heygen_video_created", "heygen_video_downloaded"],
    }


def render(
    *,
    script: str,
    output: Path,
    intent_path: Path | None = None,
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
    intent_path = Path(intent_path) if intent_path is not None else output.with_name(f".{output.name}.heygen-effect.json")
    staging = output.with_name(f".{output.name}.part")
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
    request_sha256 = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
    expected = {"schema_version": "marketing.heygen-effect.v1", "renderer_id": "heygen-avatar-iv",
                "request_sha256": request_sha256, "output": str(output)}
    intent_path.parent.mkdir(parents=True, exist_ok=True)
    if not intent_path.exists():
        require(not output.exists(), "HeyGen render output already exists without a completed receipt")
    try:
        descriptor = os.open(intent_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        require(all(intent.get(key) == value for key, value in expected.items()), "conflicting HeyGen effect replay")
        if intent.get("state") == "completed":
            video_id = str(intent.get("video_id") or "")
            require(IDENTIFIER.fullmatch(video_id) is not None and output.is_file() and output.stat().st_size > 0,
                    "completed HeyGen effect receipt invalid")
            receipt = _receipt(output, video_id)
            require(receipt["sha256"] == intent.get("output_sha256"), "completed HeyGen output differs")
            return receipt
        if intent.get("state") != "provider_created":
            return {"renderer_id": "heygen-avatar-iv", "state": "reconciliation_required",
                    "effect_key": request_sha256, "external_effects": []}
        video_id = str(intent.get("video_id") or "")
        require(IDENTIFIER.fullmatch(video_id) is not None, "HeyGen provider receipt invalid")
        if output.exists():
            require(output.is_file() and not output.is_symlink() and output.stat().st_size > 0,
                    "HeyGen recovered output invalid")
            receipt = _receipt(output, video_id)
            _write_json(intent_path, {**expected, "state": "completed", "video_id": video_id,
                                      "output_sha256": receipt["sha256"]})
            return receipt
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({**expected, "state": "prepared"}, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _write_json(intent_path, {**expected, "state": "sending"})
        try:
            created = _run(
                executor,
                [config["cli"], "video", "create", "-d", "-", "--wait"],
                input_text=json.dumps(request, ensure_ascii=False),
            )
            response = json.loads(created.stdout)
            data = response.get("data", response)
            video_id = str(data.get("video_id") or "")
            require(IDENTIFIER.fullmatch(video_id) is not None, "HeyGen create receipt has no video ID")
            require(data.get("status") == "completed", "HeyGen video did not complete")
        except BaseException:
            _write_json(intent_path, {**expected, "state": "delivery_uncertain"})
            raise
        _write_json(intent_path, {**expected, "state": "provider_created", "video_id": video_id})
    require(not output.exists(), "HeyGen render output already exists without a completed receipt")
    if staging.exists():
        staging.unlink()
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
    receipt = _receipt(output, video_id)
    _write_json(intent_path, {**expected, "state": "completed", "video_id": video_id,
                              "output_sha256": receipt["sha256"]})
    return receipt
