#!/usr/bin/env python3
"""Render a no-network Japanese watercolor motion preview from owned cached clips."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile

WATERCOLOR_CLIP_NAMES = (
    "jp_kling_clip_02.mp4", "jp_kling_clip_03.mp4", "jp_kling_clip_05.mp4",
    "jp_kling_clip_07.mp4", "jp_kling_clip_08.mp4", "jp_kling_clip_10.mp4",
)


def default_asset_root() -> pathlib.Path:
    configured = os.environ.get("LM_EBOOK_ASSET_ROOT")
    if configured:
        return pathlib.Path(configured).expanduser()
    data_home = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local/share"))
    return data_home / "life-manager/ebook-assets"


FFMPEG = os.environ.get("FFMPEG_BIN", "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")


def ass_time(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    rest = seconds % 60
    return f"{hours}:{minutes:02d}:{rest:05.2f}"


def wrap_ja(value: str, width: int = 13) -> str:
    text = value.strip()
    chunks = [text[index:index + width] for index in range(0, len(text), width)]
    closing = "。、！？!?)]）】』」"
    lines: list[str] = []
    for chunk in chunks:
        while chunk and chunk[0] in closing and lines:
            lines[-1] += chunk[0]
            chunk = chunk[1:]
        if chunk:
            lines.append(chunk)
    return r"\N".join(lines)


def duration(path: pathlib.Path) -> float:
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def subtitle_filter(ass: pathlib.Path, phrases: list[str], segment: float, work: pathlib.Path) -> str:
    filters = subprocess.run([FFMPEG, "-filters"], capture_output=True, text=True, check=True).stdout
    if "subtitles" in filters:
        path = str(ass).replace(":", r"\:").replace("'", r"\'")
        return f"subtitles=filename='{path}'"
    lines: list[str] = []
    for index, phrase in enumerate(phrases):
        textfile = work / f"caption-{index}.txt"
        textfile.write_text(wrap_ja(phrase).replace(r"\N", "\n"), encoding="utf-8")
        start, end = index * segment, (index + 1) * segment
        lines.append(
            "drawtext=fontfile='/System/Library/Fonts/Hiragino Sans GB.ttc'"
            f":textfile='{textfile}':fontcolor=white:fontsize=50:borderw=3:bordercolor=0x141414"
            f":x=(w-text_w)/2:y=h-180:enable='between(t,{start:.3f},{end:.3f})'"
        )
    return ",".join(lines)


def render(*, script: str, output: pathlib.Path, clips: list[pathlib.Path],
           voice: str = "Kyoko", voice_rate: int = 165,
           caption_style_id: str = "ass.watercolor.safe-v1") -> dict:
    if not clips or any(not path.is_file() for path in clips):
        raise ValueError("all cached motion clips must exist")
    if caption_style_id != "ass.watercolor.safe-v1":
        raise ValueError("unsupported caption style")
    output = pathlib.Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="watercolor-preview-") as raw:
        work = pathlib.Path(raw)
        audio = work / "voice.aiff"
        ass = work / "captions.ass"
        listing = work / "clips.txt"
        subprocess.run(["say", "-v", voice, "-r", str(voice_rate), "-o", str(audio), script],
                       check=True, capture_output=True)
        audio_duration = duration(audio)
        phrases = [piece.strip() for piece in script.replace("。", "。|").split("|") if piece.strip()]
        segment = audio_duration / len(phrases)
        events = []
        for index, phrase in enumerate(phrases):
            events.append(
                f"Dialogue: 0,{ass_time(index * segment)},{ass_time((index + 1) * segment)},Default,,0,0,0,,{wrap_ja(phrase)}"
            )
        ass.write_text("""[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Hiragino Sans,50,&H00FFFFFF,&H000000FF,&H00141414,&H99000000,-1,0,0,0,100,100,1,0,3,3,0,2,64,64,150,1
[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
""" + "\n".join(events) + "\n", encoding="utf-8")
        enough = []
        while len(enough) * 5 < audio_duration + 5:
            enough.extend(clips)
        listing.write_text("".join(f"file '{path}'\n" for path in enough), encoding="utf-8")
        video_filter = subtitle_filter(ass, phrases, segment, work)
        command = [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
            "-safe", "0", "-i", str(listing), "-i", str(audio),
            "-vf", video_filter, "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart", str(output)
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode:
            raise RuntimeError(f"ffmpeg render failed: {completed.stderr.strip()}")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {"status": "rendered_preview", "output": str(output), "sha256": digest,
            "duration": round(duration(output), 3), "external_cost_usd": 0,
            "external_effects": [], "voice": voice, "voice_rate": voice_rate,
            "caption_style_id": caption_style_id}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--asset-root", type=pathlib.Path)
    args = parser.parse_args()
    clips_root = (args.asset_root or default_asset_root()) / "watercolor-monk/clips"
    clips = [clips_root / name for name in WATERCOLOR_CLIP_NAMES]
    print(json.dumps(render(script=args.script, output=args.output, clips=clips),
                     ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
