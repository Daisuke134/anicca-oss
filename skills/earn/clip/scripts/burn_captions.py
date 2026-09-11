#!/usr/bin/env python3
"""Burn word-level karaoke captions onto a 9:16 clip.

Transcribes the clip itself (clip-relative timing) with faster-whisper
word_timestamps=True, builds a karaoke-style ASS subtitle (current word
highlighted), and burns it in with ffmpeg. Optionally adds a hook banner
over the first N seconds.

Usage:
  burn_captions.py <in.mp4> <out.mp4> [--hook "TEXT"] [--model tiny] [--lang en]

Install the repository-declared dependencies, then run with the active Python:
  python3 -m pip install -r skills/earn/clip/requirements.txt
  python3 burn_captions.py ...
"""
import argparse, os, subprocess, sys, tempfile


def fmt_ass_ts(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    cs = int(round((s - int(s)) * 100))
    return f"{h:d}:{m:02d}:{int(s):02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def build_ass(words, hook, width, height):
    """words = list of (start, end, text). Karaoke: show a rolling window of
    ~5 words, current word in accent color."""
    play_w, play_h = width, height
    cap_fs = max(16, round(play_w * 0.085))
    hook_fs = max(14, round(play_w * 0.072))
    margin_lr = max(12, round(play_w * 0.06))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Arial Black,{cap_fs},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,3,5,{margin_lr},{margin_lr},0,1
Style: Hook,Arial Black,{hook_fs},&H0000F0FF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,4,3,8,{margin_lr},{margin_lr},70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    accent = "&H0000F0FF"  # amber-ish BGR
    white = "&H00FFFFFF"
    n = len(words)
    WIN = 3
    for i, (st, en, txt) in enumerate(words):
        # rolling window centered on current word
        lo = max(0, i - WIN // 2)
        hi = min(n, lo + WIN)
        lo = max(0, hi - WIN)
        parts = []
        for j in range(lo, hi):
            w = ass_escape(words[j][2])
            if j == i:
                parts.append(f"{{\\c{accent}\\fscx112\\fscy112}}{w}{{\\c{white}\\fscx100\\fscy100}}")
            else:
                parts.append(w)
        text = " ".join(parts).strip()
        lines.append(
            f"Dialogue: 0,{fmt_ass_ts(st)},{fmt_ass_ts(en)},Cap,,0,0,0,,{text}"
        )
    if hook:
        hk = ass_escape(hook.upper())
        lines.append(
            f"Dialogue: 1,{fmt_ass_ts(0.0)},{fmt_ass_ts(2.0)},Hook,,0,0,0,,{hk}"
        )
    return head + "\n".join(lines) + "\n"


def transcribe_words(clip, model_size, lang):
    from faster_whisper import WhisperModel
    m = WhisperModel(model_size, device="cpu", compute_type="int8")
    segs, _ = m.transcribe(clip, language=lang, word_timestamps=True)
    words = []
    for s in segs:
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                words.append((float(w.start), float(w.end), t))
    return words


def transcribe_segments(clip, model_size, lang):
    """Segment-level (phrase) transcription — for JP subtitles where word-by-word
    karaoke timing from EN audio doesn't map to translated text."""
    from faster_whisper import WhisperModel
    m = WhisperModel(model_size, device="cpu", compute_type="int8")
    segs, _ = m.transcribe(clip, language=lang, word_timestamps=False)
    return [(float(s.start), float(s.end), s.text.strip()) for s in segs if s.text.strip()]


def translate_segments_ja(segments):
    """Translate EN segment texts → natural JP via Gemini. Returns list of
    (start, end, jp_text). No-human, deterministic call."""
    import os, json, urllib.request
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY/GOOGLE_API_KEY required for --jp")
    texts = [s[2] for s in segments]
    prompt = (
        "Translate each numbered English line into natural, punchy Japanese suitable "
        "for a short-video subtitle. Keep it concise. Return ONLY a JSON array of "
        "strings, same length and order, no numbering.\n\n"
        + "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    )
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"}}
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           "gemini-2.5-flash:generateContent?key=" + key)
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    raw = resp["candidates"][0]["content"]["parts"][0]["text"]
    ja = json.loads(raw)
    out = []
    for i, (st, en, _) in enumerate(segments):
        out.append((st, en, ja[i] if i < len(ja) else ""))
    return out


def build_ass_segments(segments, hook, width, height):
    """Phrase-level centered subtitles (used for JP). One dialogue per segment."""
    play_w, play_h = width, height
    cap_fs = max(18, round(play_w * 0.075))
    hook_fs = max(14, round(play_w * 0.072))
    margin_lr = max(12, round(play_w * 0.06))
    # Use a CJK-capable font
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Hiragino Sans,{cap_fs},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,3,5,{margin_lr},{margin_lr},0,1
Style: Hook,Hiragino Sans,{hook_fs},&H0000F0FF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,4,3,8,{margin_lr},{margin_lr},70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def wrap_jp(s, per_line=11):
        # hard-wrap CJK text to per_line chars using ASS \N, so it never
        # overflows a narrow vertical frame
        out, line = [], ""
        for ch in s:
            line += ch
            if len(line) >= per_line and ch in "。、！？!?, 　 ":
                out.append(line); line = ""
            elif len(line) >= per_line + 4:
                out.append(line); line = ""
        if line:
            out.append(line)
        return "\\N".join(out)

    lines = []
    for st, en, txt in segments:
        if not txt:
            continue
        wrapped = wrap_jp(ass_escape(txt))
        lines.append(f"Dialogue: 0,{fmt_ass_ts(st)},{fmt_ass_ts(en)},Cap,,0,0,0,,{wrapped}")
    if hook:
        lines.append(f"Dialogue: 1,{fmt_ass_ts(0.0)},{fmt_ass_ts(2.0)},Hook,,0,0,0,,{ass_escape(hook)}")
    return head + "\n".join(lines) + "\n"


def probe_dims(clip):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", clip],
        capture_output=True, text=True,
    ).stdout.strip()
    w, h = out.split("x")
    return int(w), int(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("outp")
    ap.add_argument("--hook", default=None)
    ap.add_argument("--model", default="tiny")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--jp", action="store_true", help="Translate to JP + phrase-level centered subs (jimaku)")
    a = ap.parse_args()

    w, h = probe_dims(a.inp)
    print(f"[burn] dims {w}x{h}", file=sys.stderr)
    if a.jp:
        segs = transcribe_segments(a.inp, a.model, a.lang)
        print(f"[burn] {len(segs)} EN segments → translating to JP", file=sys.stderr)
        ja = translate_segments_ja(segs)
        print(f"[burn] sample JP: {ja[0][2] if ja else '(none)'}", file=sys.stderr)
        ass = build_ass_segments(ja, a.hook, w, h)
    else:
        words = transcribe_words(a.inp, a.model, a.lang)
        print(f"[burn] {len(words)} words", file=sys.stderr)
        ass = build_ass(words, a.hook, w, h)
    with tempfile.NamedTemporaryFile("w", suffix=".ass", delete=False) as f:
        f.write(ass)
        ass_path = f.name
    # burn in
    rc = subprocess.run([
        "ffmpeg", "-y", "-i", a.inp,
        "-vf", f"ass={ass_path}",
        # Mixing -crf with -b:v/-minrate puts libx264 in capped-CRF mode, where CRF
        # still drives bit allocation and the bitrate flags only cap VBV bursts --
        # they do NOT force a floor on low-motion content (verified empirically
        # 2026-07-11: a synthetic low-complexity clip landed at the same ~17kb/s
        # under both crf-only and crf+b:v encodes). Two real producer renders hit
        # verify_clip.sh's 2.5Mbps floor at ~2.18Mbps even after adding the
        # crf+b:v flags, confirming the combo doesn't work. Drop -crf entirely and
        # use pure ABR (-b:v as the real target) instead, which does guarantee an
        # average bitrate close to the target regardless of source complexity.
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", "4000k", "-maxrate", "4500k", "-bufsize", "8000k",
        "-c:a", "copy", "-movflags", "+faststart", a.outp,
    ], capture_output=True, text=True)
    os.unlink(ass_path)
    if rc.returncode != 0:
        print("ffmpeg failed:\n" + rc.stderr[-800:], file=sys.stderr)
        sys.exit(1)
    print(a.outp)


if __name__ == "__main__":
    main()
