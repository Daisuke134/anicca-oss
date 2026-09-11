from __future__ import annotations

import json
from pathlib import Path
import subprocess
import shutil
import heygen_candidate

from heygen_candidate import build_request, configuration, render


READY = {
    "LIFE_MANAGER_HEYGEN": shutil.which("heygen") or "/usr/bin/true",
    "LM_EBOOK_EN_HEYGEN_AVATAR_ID": "avatar_12345678",
    "LM_EBOOK_EN_HEYGEN_VOICE_ID": "voice_12345678",
}


def test_missing_private_configuration_is_setup_required_without_effect(tmp_path, monkeypatch):
    monkeypatch.setattr("heygen_candidate.shutil.which", lambda _name: None)
    calls = []
    receipt = render(
        script="Breathe.", output=tmp_path / "video.mp4", environment={},
        executor=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    assert receipt == {
        "renderer_id": "heygen-avatar-iv",
        "state": "setup_required",
        "missing": ["LM_EBOOK_EN_HEYGEN_AVATAR_ID", "LM_EBOOK_EN_HEYGEN_VOICE_ID", "heygen_cli"],
        "external_effects": [],
    }
    assert calls == []


def test_request_is_vertical_avatar_iv_and_contains_no_credential():
    config = configuration(READY)
    request = build_request("  Breathe slowly.  ", config)
    assert request == {
        "type": "avatar", "avatar_id": "avatar_12345678", "script": "Breathe slowly.",
        "voice_id": "voice_12345678", "voice_settings": {"speed": 0.9, "locale": "en-US"},
        "engine": {"type": "avatar_iv"}, "aspect_ratio": "9:16", "resolution": "1080p",
        "output_format": "mp4",
    }
    assert "key" not in json.dumps(request).lower()


def test_missing_login_is_setup_required_before_create(tmp_path):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)

    receipt = render(
        script="Breathe.", output=tmp_path / "video.mp4",
        environment=READY, executor=execute,
    )
    assert receipt == {
        "renderer_id": "heygen-avatar-iv", "state": "setup_required",
        "missing": ["heygen_auth"], "external_effects": [],
    }
    assert calls == [[READY["LIFE_MANAGER_HEYGEN"], "auth", "status"]]


def test_render_uses_stdin_then_downloads_and_hashes_receipt(tmp_path):
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if args[2] == "create":
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "data": {"video_id": "video_12345678", "status": "completed"}
            }), "")
        Path(args[-1]).write_bytes(b"video")
        return subprocess.CompletedProcess(args, 0, "{}", "")

    output = tmp_path / "render.mp4"
    receipt = render(script="Breathe.", output=output, environment=READY, executor=execute)
    assert receipt["state"] == "rendered"
    assert receipt["video_id"] == "video_12345678"
    assert receipt["sha256"] == "0cab1c9617404faf2b24e221e189ca5945813e14d3f766345b09ca13bbe28ffc"
    assert calls[0][0] == [READY["LIFE_MANAGER_HEYGEN"], "auth", "status"]
    assert calls[1][0] == [READY["LIFE_MANAGER_HEYGEN"], "video", "create", "-d", "-", "--wait"]
    assert json.loads(calls[1][1]["input"])["script"] == "Breathe."
    assert "Breathe." not in " ".join(calls[1][0])
    assert calls[2][0] == [
        READY["LIFE_MANAGER_HEYGEN"], "video", "download", "video_12345678",
        "--output-path", str(output.with_name(f".{output.name}.part")),
    ]
    assert not output.with_name(f".{output.name}.part").exists()


def test_unknown_create_outcome_is_durable_and_replay_never_creates_again(tmp_path):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        raise RuntimeError("connection lost after create request")

    output = tmp_path / "unknown.mp4"
    intent = tmp_path / "effect.json"
    try:
        render(script="Breathe.", output=output, intent_path=intent,
               environment=READY, executor=execute)
    except RuntimeError:
        pass
    else:
        raise AssertionError("unknown provider outcome must not become success")
    assert json.loads(intent.read_text())["state"] == "delivery_uncertain"

    replay = render(script="Breathe.", output=output, intent_path=intent,
                    environment=READY, executor=execute)
    assert replay["state"] == "reconciliation_required"
    assert sum(args[2] == "create" for args in calls if len(args) > 2) == 1


def test_download_failure_resumes_from_stored_video_id_without_second_create(tmp_path):
    calls = []
    fail_download = True

    def execute(args, **kwargs):
        nonlocal fail_download
        calls.append(args)
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if args[2] == "create":
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "data": {"video_id": "video_12345678", "status": "completed"}
            }), "")
        if fail_download:
            fail_download = False
            raise RuntimeError("download interrupted")
        Path(args[-1]).write_bytes(b"video")
        return subprocess.CompletedProcess(args, 0, "{}", "")

    output = tmp_path / "resumed.mp4"
    intent = tmp_path / "effect.json"
    try:
        render(script="Breathe.", output=output, intent_path=intent,
               environment=READY, executor=execute)
    except RuntimeError:
        pass
    else:
        raise AssertionError("first download must fail")
    assert json.loads(intent.read_text())["state"] == "provider_created"

    replay = render(script="Breathe.", output=output, intent_path=intent,
                    environment=READY, executor=execute)
    assert replay["state"] == "rendered"
    assert sum(args[2] == "create" for args in calls if len(args) > 2) == 1
    assert json.loads(intent.read_text())["state"] == "completed"


def test_replay_recovers_crash_after_output_rename_without_second_create(tmp_path, monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, "{}", "")
        if args[2] == "create":
            return subprocess.CompletedProcess(args, 0, json.dumps({
                "data": {"video_id": "video_12345678", "status": "completed"}
            }), "")
        Path(args[-1]).write_bytes(b"video")
        return subprocess.CompletedProcess(args, 0, "{}", "")

    output = tmp_path / "renamed.mp4"
    intent = tmp_path / "effect.json"
    real_write = heygen_candidate._write_json

    def crash_before_completed_receipt(path, value):
        if value.get("state") == "completed":
            raise RuntimeError("crash after output rename")
        real_write(path, value)

    monkeypatch.setattr(heygen_candidate, "_write_json", crash_before_completed_receipt)
    try:
        render(script="Breathe.", output=output, intent_path=intent,
               environment=READY, executor=execute)
    except RuntimeError:
        pass
    else:
        raise AssertionError("receipt write must crash")
    assert output.read_bytes() == b"video"
    assert json.loads(intent.read_text())["state"] == "provider_created"

    monkeypatch.setattr(heygen_candidate, "_write_json", real_write)
    replay = render(script="Breathe.", output=output, intent_path=intent,
                    environment=READY, executor=execute)
    assert replay["state"] == "rendered"
    assert sum(args[2] == "create" for args in calls if len(args) > 2) == 1
    assert sum(args[2] == "download" for args in calls if len(args) > 2) == 1
    assert json.loads(intent.read_text())["state"] == "completed"
