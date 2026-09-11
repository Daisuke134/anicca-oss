from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import watercolor_candidate as candidate
from watercolor_candidate import wrap_ja


def test_wrap_ja_never_leaves_japanese_punctuation_alone():
    text = "あなたを小さくする関係を見抜いて、守るべき人と時間に集中するための5つのサイン。"
    wrapped = wrap_ja(text, width=13)
    lines = wrapped.split(r"\N")
    assert "".join(lines) == text
    assert all(line not in {"。", "、", "！", "？", "!", "?"} for line in lines)


def test_wrap_ja_keeps_normal_short_text_unchanged():
    assert wrap_ja("短い字幕。", width=13) == "短い字幕。"


def test_missing_portable_media_tools_is_structured_setup_required(tmp_path, monkeypatch):
    clips = []
    for name in candidate.WATERCOLOR_CLIP_NAMES:
        clip = tmp_path / name
        clip.write_bytes(b"clip")
        clips.append(clip)
    monkeypatch.setattr(candidate, "resolve_ffmpeg", lambda: None)
    monkeypatch.setattr(candidate, "resolve_ffprobe", lambda: None)
    monkeypatch.setattr(candidate.shutil, "which", lambda _name: None)
    receipt = candidate.render(script="呼吸します。", output=tmp_path / "out.mp4", clips=clips)
    assert receipt == {
        "renderer_id": "watercolor-monk", "state": "setup_required",
        "missing": ["ffmpeg", "ffprobe", "text_to_speech"], "external_effects": [],
    }
    assert not (tmp_path / "out.mp4").exists()


def test_invalid_configured_tts_is_structured_setup_required(tmp_path, monkeypatch):
    clips = []
    for name in candidate.WATERCOLOR_CLIP_NAMES:
        clip = tmp_path / name
        clip.write_bytes(b"clip")
        clips.append(clip)
    monkeypatch.setattr(candidate, "resolve_ffmpeg", lambda: "/usr/bin/true")
    monkeypatch.setattr(candidate, "resolve_ffprobe", lambda: "/usr/bin/true")
    monkeypatch.setenv("LIFE_MANAGER_SAY", "/definitely/missing/tts")
    receipt = candidate.render(script="呼吸します。", output=tmp_path / "out.mp4", clips=clips)
    assert receipt["state"] == "setup_required"
    assert receipt["missing"] == ["text_to_speech"]
    assert receipt["external_effects"] == []
    assert not (tmp_path / "out.mp4").exists()


def test_watercolor_uses_neutral_versioned_pack_names():
    assert candidate.WATERCOLOR_CLIP_NAMES == tuple(f"scene-{number:02d}.mp4" for number in range(1, 7))
    assert candidate.default_pack_root(pathlib.Path("/portable/assets")) == pathlib.Path(
        "/portable/assets/packs/default-v1"
    )
