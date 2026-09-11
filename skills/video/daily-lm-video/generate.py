#!/usr/bin/env python3
"""Render one daily Life Manager vertical video from the canonical creative bank."""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path


FIELDS = {"id", "pain", "moment", "punchline", "material_hint"}
EXPECTED_IDS = [f"A{i:02d}" for i in range(1, 7)] + [f"B{i:02d}" for i in range(1, 8)] + [f"C{i:02d}" for i in range(1, 4)]


def load_bank(path):
    rows = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"malformed bank line {number}: {error.msg}") from error
            # The bank now also carries English beats for the narration loop, so require the core
            # fields rather than forbidding every additional one.
            if not isinstance(row, dict) or not FIELDS.issubset(set(row)):
                raise ValueError(f"invalid bank schema at line {number}")
            if any(not isinstance(row[field], str) or not row[field].strip() for field in FIELDS):
                raise ValueError(f"empty bank field at line {number}")
            rows.append(row)
    ids = [row["id"] for row in rows]
    if ids != EXPECTED_IDS:
        raise ValueError("creative bank must contain the canonical 16 A/B/C rows in order")
    return rows


def load_state(path, valid_ids):
    path = Path(path)
    if not path.exists():
        return []
    records = []
    required = {"ts", "date", "creative_id", "cycle", "output", "duration_seconds"}
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"malformed state line {number}: {error.msg}") from error
            if not isinstance(record, dict) or not required.issubset(record):
                raise ValueError(f"invalid state schema at line {number}")
            if record["creative_id"] not in valid_ids or not isinstance(record["cycle"], int) or record["cycle"] < 1:
                raise ValueError(f"invalid state value at line {number}")
            records.append(record)
    return records


def select_by_performance(_rows, _records):
    """9d hook: return a creative row when performance data becomes authoritative."""
    return None


def select_creative(rows, records):
    selected = select_by_performance(rows, records)
    if selected is not None:
        return selected
    cycle = max((record["cycle"] for record in records), default=1)
    used = {record["creative_id"] for record in records if record["cycle"] == cycle}
    if len(used) >= len(rows):
        cycle += 1
        used = set()
    return next(row for row in rows if row["id"] not in used), cycle


def ass_text(value, width=23):
    value = value.replace("\\", "／").replace("{", "（").replace("}", "）")
    return r"\N".join(textwrap.wrap(value, width=width, break_long_words=True, break_on_hyphens=False))


def write_creative_ass(path, row, duration):
    end = f"0:00:{duration:05.2f}"
    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 1

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Label,Noto Sans CJK JP,35,&H00B8F7DA,&H000000FF,&H0011161F,&HA0000000,1,0,0,0,100,100,2,0,1,2,0,8,60,60,110,1
Style: Main,Noto Sans CJK JP,64,&H00FFFFFF,&H000000FF,&H0011161F,&HC0000000,1,0,0,0,100,100,0,0,1,5,0,5,70,70,0,1
Style: Small,Noto Sans CJK JP,38,&H00E2E8F0,&H000000FF,&H0011161F,&HA0000000,1,0,0,0,100,100,0,0,1,3,0,8,65,65,150,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 1,0:00:00.00,0:00:05.00,Label,,0,0,0,,BEFORE LIFE MANAGER • {row['id']}
Dialogue: 1,0:00:00.20,0:00:05.00,Main,,0,0,0,,{ass_text(row['pain'])}
Dialogue: 1,0:00:15.00,0:00:26.00,Small,,0,0,0,,{ass_text(row['moment'], 28)}
Dialogue: 2,0:00:31.00,{end},Main,,0,0,0,,{ass_text(row['punchline'])}
"""
    Path(path).write_text(content, encoding="utf-8")


def shifted_ass_time(value, offset_seconds):
    hours, minutes, seconds = value.split(":")
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds) + offset_seconds
    shifted_hours = int(total // 3600)
    shifted_minutes = int((total % 3600) // 60)
    shifted_seconds = total % 60
    return f"{shifted_hours}:{shifted_minutes:02d}:{shifted_seconds:05.2f}"


def write_whisper_only_ass(source, destination, offset_seconds=5):
    """Keep only speech events and shift them to match the delayed production audio."""
    output = []
    dialogue_count = 0
    for line in Path(source).read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue:"):
            output.append(line)
            continue
        fields = line.split(",", 9)
        if len(fields) != 10 or fields[3] not in {"Default", "Caption"}:
            continue
        fields[1] = shifted_ass_time(fields[1], offset_seconds)
        fields[2] = shifted_ass_time(fields[2], offset_seconds)
        output.append(",".join(fields))
        dialogue_count += 1
    if dialogue_count == 0:
        raise ValueError("Whisper ASS contains no speech dialogue events")
    Path(destination).write_text("\n".join(output) + "\n", encoding="utf-8")


def validate_render(output, ffprobe_bin):
    output = Path(output)
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("render did not produce a non-empty MP4")
    result = subprocess.run(
        [ffprobe_bin, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
        check=True,
        text=True,
        capture_output=True,
    )
    try:
        probe = json.loads(result.stdout)
        streams = probe["streams"]
        duration = float(probe["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("ffprobe returned malformed metadata") from error
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video or video.get("codec_name") != "h264" or video.get("width") != 1080 or video.get("height") != 1920:
        raise RuntimeError("render is not 1080x1920 H.264")
    if not audio or audio.get("codec_name") != "aac":
        raise RuntimeError("render has no AAC audio")
    if not 20 <= duration <= 40:
        raise RuntimeError(f"render duration outside 20-40 seconds: {duration}")
    return duration


def append_state(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def render(args, row, output):
    for label, value in (
        ("call audio", args.call_audio),
        ("stock", args.stock),
        ("Telegram proof", args.telegram_proof),
        ("Whisper ASS", args.whisper_ass),
    ):
        if not value.is_file() or value.stat().st_size == 0:
            raise FileNotFoundError(f"missing input ({label}): {value}")
    for label, binary in (("ffmpeg", args.ffmpeg_bin), ("ffprobe", args.ffprobe_bin)):
        if os.path.sep in binary:
            if not Path(binary).is_file():
                raise FileNotFoundError(f"missing input ({label}): {binary}")
        elif shutil.which(binary) is None:
            raise FileNotFoundError(f"missing input ({label}): {binary}")

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(".partial.mp4")
    partial.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="daily-lm-video-") as temporary:
        temporary_path = Path(temporary)
        write_whisper_only_ass(args.whisper_ass, temporary_path / "whisper.ass", offset_seconds=5)
        write_creative_ass(temporary_path / "creative.ass", row, args.duration)
        filters = f"""
[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30,trim=duration={args.duration},setpts=PTS-STARTPTS,eq=brightness=-0.12:contrast=1.08:saturation=0.72[footage];
[3:v]format=rgba,colorchannelmixer=aa=0.96[finalbg];
[footage][finalbg]overlay=enable='gte(t,26)'[scene];
[2:v]scale=960:-1:flags=lanczos[proof];
[scene][proof]overlay=x=(W-w)/2:y=735:enable='between(t,26,{args.duration})'[withproof];
[withproof]ass=whisper.ass,ass=creative.ass[v];
[1:a]adelay=5000|5000,loudnorm=I=-16:TP=-1.5:LRA=11,apad=whole_dur={args.duration},atrim=duration={args.duration}[a]
""".strip()
        command = [
            args.ffmpeg_bin, "-hide_banner", "-y", "-stream_loop", "-1", "-i", str(args.stock),
            "-i", str(args.call_audio), "-loop", "1", "-i", str(args.telegram_proof),
            "-f", "lavfi", "-i", f"color=c=0x10151e:s=1080x1920:r=30:d={args.duration}",
            "-filter_complex", filters, "-map", "[v]", "-map", "[a]", "-t", str(args.duration),
            "-r", "30", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(partial),
        ]
        try:
            subprocess.run(command, cwd=temporary_path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            duration = validate_render(partial, args.ffprobe_bin)
            partial.replace(output)
            return duration
        finally:
            partial.unlink(missing_ok=True)


def default_video_root():
    # Defaults live beneath the portable Life Manager data root (LM_DATA_DIR,
    # falling back to <home>/.local/state/life-manager), never a legacy root.
    # The <data root>/state/lm-video layout is the single convention shared with
    # skills/life-manager/life-manager-daily.sh and the dev-loop state dir.
    data_root = Path(os.environ.get("LM_DATA_DIR") or Path.home() / ".local/state/life-manager")
    return data_root / "state" / "lm-video"


def legacy_video_state_root():
    # Named ONLY to detect unmigrated state and refuse to run (fail-loud guard);
    # this generator never reads the legacy store.
    legacy_state = Path(os.environ.get("LM_LEGACY_STATE_ROOT") or Path.home() / ".openclaw/state")
    return legacy_state / "lm-video"


def guard_unmigrated_legacy_state(args):
    """Fail loudly when a default-rooted state/input is absent but its legacy
    counterpart still exists: silently starting empty (or silently reading the
    legacy path) would lose the rotation history and recordings."""
    video_root = default_video_root()
    legacy_root = legacy_video_state_root()
    for label, new_path in (
        ("rotation state", args.state),
        ("call audio", args.call_audio),
        ("Whisper ASS", args.whisper_ass),
    ):
        new_path = Path(new_path)
        try:
            relative = new_path.relative_to(video_root)
        except ValueError:
            continue
        legacy_path = legacy_root / relative
        if not new_path.exists() and legacy_path.exists():
            raise SystemExit(
                f"unmigrated legacy state: {label} is missing at {new_path} but still exists at "
                f"{legacy_path}; run apps/life-manager/scripts/migrate-legacy-state.sh "
                "(copy-only, the legacy store stays intact) and re-run"
            )


def parser():
    here = Path(__file__).resolve().parent
    parse = argparse.ArgumentParser()
    parse.add_argument("--bank", type=Path, default=Path(os.environ.get("LM_VIDEO_BANK", here / "creative-bank.jsonl")))
    video_root = default_video_root()
    parse.add_argument("--state", type=Path, default=Path(os.environ.get("LM_VIDEO_STATE", video_root / "daily-render-state.jsonl")))
    parse.add_argument("--output-dir", type=Path, default=Path(os.environ.get("LM_VIDEO_OUTPUT_DIR", video_root / "daily-renders")))
    parse.add_argument("--call-audio", type=Path, default=Path(os.environ.get("LM_VIDEO_CALL_AUDIO", video_root / "recordings/2026-07-19T23-40-35-932b3fad-2a99-49ca-8250-3a49e682ce92.mp3")))
    parse.add_argument("--stock", type=Path, default=Path(os.environ.get("LM_VIDEO_STOCK", video_root / "inputs/stock.mp4")))
    parse.add_argument("--telegram-proof", type=Path, default=Path(os.environ.get("LM_VIDEO_TELEGRAM_PROOF", video_root / "inputs/telegram-proof.png")))
    parse.add_argument("--whisper-ass", type=Path, default=Path(os.environ.get("LM_VIDEO_WHISPER_ASS", video_root / "2026-07-20/2026-07-19T23-40-35-932b3fad-2a99-49ca-8250-3a49e682ce92.ass")))
    parse.add_argument("--ffmpeg-bin", default=os.environ.get("LM_VIDEO_FFMPEG", "ffmpeg"))
    parse.add_argument("--ffprobe-bin", default=os.environ.get("LM_VIDEO_FFPROBE", "ffprobe"))
    parse.add_argument("--duration", type=float, default=float(os.environ.get("LM_VIDEO_DURATION", "34.656")))
    parse.add_argument("--date", default=os.environ.get("LM_VIDEO_DATE", datetime.now().astimezone().date().isoformat()))
    return parse


def main(argv=None):
    args = parser().parse_args(argv)
    guard_unmigrated_legacy_state(args)
    rows = load_bank(args.bank)
    valid_ids = {row["id"] for row in rows}
    lock_path = Path(str(args.state) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = load_state(args.state, valid_ids)
        today = next((record for record in reversed(records) if record["date"] == args.date), None)
        if today:
            duration = validate_render(Path(today["output"]), args.ffprobe_bin)
            print(json.dumps({"selected_id": today["creative_id"], "output": today["output"], "duration_seconds": duration}, ensure_ascii=False, separators=(",", ":")))
            return 0
        row, cycle = select_creative(rows, records)
        output = args.output_dir / f"{args.date}-{row['id']}.mp4"
        duration = render(args, row, output)
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "date": args.date,
            "creative_id": row["id"],
            "cycle": cycle,
            "output": str(output.resolve()),
            "duration_seconds": duration,
        }
        append_state(args.state, record)
        print(json.dumps({"selected_id": row["id"], "output": record["output"], "duration_seconds": duration}, ensure_ascii=False, separators=(",", ":")))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"daily-lm-video error: {error}", file=sys.stderr)
        raise SystemExit(1)
