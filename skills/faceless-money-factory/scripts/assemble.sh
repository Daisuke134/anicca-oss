#!/usr/bin/env bash
# assemble.sh <voice.mp3> <broll_dir> <out.mp4> [lang] — build the final faceless short.
# Beat-aligned: cuts the background on the voice's sentence boundaries (whisper segments) so the
# background changes WITH the narration (no lag). Each clip is LOOPED to fill its beat, and the
# montage covers the FULL voice length, so the ending (CTA) is never cut. Then burns captions.
# Verified approach (money_v2). $0 (local ffmpeg + whisper).
set -euo pipefail
VOICE="${1:?voice.mp3 required}"; BROLL="${2:?broll_dir required}"; OUT="${3:?out.mp4 required}"; LANG_CODE="${4:-en}"
export PATH="$HOME/.local/bin:$PATH"
SK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON_BIN="${LIFE_MANAGER_PYTHON:-$(command -v python3 2>/dev/null)}"
[ -n "$PYTHON_BIN" ] || { echo "python3 is required" >&2; exit 2; }
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

# 1) voice beats (whisper segments)
whisper "$VOICE" --model base --language "$LANG_CODE" --output_format json --output_dir "$WORK" --verbose False >/dev/null 2>&1 || true
JSON="$WORK/$(basename "${VOICE%.*}").json"
[ -s "$JSON" ] || { echo "WHISPER_FAILED" >&2; exit 3; }

# 2) one vertical b-roll segment per beat, clip cycled, looped to exact beat length
"$PYTHON_BIN" - "$JSON" "$BROLL" "$WORK" <<'PY'
import json, sys, os, subprocess, glob
segs = json.load(open(sys.argv[1]))["segments"]
broll = sorted(glob.glob(os.path.join(sys.argv[2], "*.mp4")))
work = sys.argv[3]
if not broll: print("NO_BROLL", file=sys.stderr); sys.exit(4)
files=[]
for i,s in enumerate(segs):
    ln = round(float(s["end"]) - float(s["start"]), 2)
    if ln <= 0.2: ln = 0.5
    clip = broll[i % len(broll)]
    out = os.path.join(work, f"seg{i:02d}.mp4")
    subprocess.run(["ffmpeg","-y","-stream_loop","-1","-t",str(ln),"-i",clip,
        "-vf","scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30",
        "-an","-c:v","libx264","-preset","fast","-crf","22",out],
        check=True, stderr=subprocess.DEVNULL)
    files.append(out)
open(os.path.join(work,"cc.txt"),"w").write("\n".join(f"file '{f}'" for f in files)+"\n")
print(f"segments={len(files)} clips={len(broll)}")
PY

# 3) concat → mux full voice (montage >= voice so the ending is never cut)
ffmpeg -y -f concat -safe 0 -i "$WORK/cc.txt" -c copy "$WORK/montage.mp4" 2>/dev/null
ffmpeg -y -i "$WORK/montage.mp4" -i "$VOICE" -map 0:v -map 1:a -c:v copy -c:a aac -b:a 160k -shortest "$WORK/base.mp4" 2>/dev/null

# 4) burn captions
bash "$SK/scripts/burn-captions.sh" "$WORK/base.mp4" "$OUT" "$LANG_CODE" >/dev/null 2>&1
[ -s "$OUT" ] || { echo "ASSEMBLE_FAILED" >&2; exit 6; }
echo "ASSEMBLE_DONE=$OUT dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT")s"
