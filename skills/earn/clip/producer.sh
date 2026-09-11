#!/usr/bin/env bash
# producer.sh — CLIP-G: the FUEL. Produce ONE fresh captioned 9:16 clip from a long-form
# source and drop it (mp4 + caption.txt) into ~/clips/queue for the loop to post. Posting is
# NOT done here (that's the earn/clip slot's job). Heavy (yt-dlp + whisper + Gemini + crop +
# burn) → run as a DAILY producer cron, separate from the hourly post loop.
#
# Uses the repository-owned pipeline.py, burn_captions.py and verify_clip.sh.
#
#   producer.sh --url <youtube_long_form>   # produce from a specific source
#   producer.sh                              # pick a trending money/AI long-form (engine default)
# Disk hygiene (HARD 0.26): all heavy intermediates go in a /tmp workdir, removed at the end;
# only the final verified mp4 + caption land in ~/clips/queue.
set -uo pipefail
# The encode scripts live beside this entrypoint in the immutable Life Manager release.
SKILLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts"
PY="${CLIP_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3 2>/dev/null)}}"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_instance_paths.sh"
QUEUE="$CLIP_QUEUE"; mkdir -p "$QUEUE"
POSTED="$CLIP_POSTED"; mkdir -p "$POSTED"
PENDING_VERIFY="$CLIP_PENDING_VERIFY"; mkdir -p "$PENDING_VERIFY"
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../../_shared/lib/load-instance-env.sh"
export LOCAL_WHISPER_MODEL=tiny
export GOOGLE_API_KEY="${GOOGLE_API_KEY:-${GEMINI_API_KEY:-}}"

URL=""; [ "${1:-}" = "--url" ] && URL="${2:-}"
emit(){ printf '{"producer":"clip","did":"%s"}\n' "$1"; }

if [ -z "$PY" ]; then
  printf '{"producer":"clip","status":"setup_required","missing":["python3"]}\n'
  exit 0
fi
if ! "$PY" -c 'import faster_whisper, yt_dlp' >/dev/null 2>&1; then
  printf '{"producer":"clip","status":"setup_required","missing":["faster-whisper","yt-dlp"],"install":"python3 -m pip install -r skills/earn/clip/requirements.txt"}\n'
  exit 0
fi

# pick a trending single-speaker money/AI long-form if no url (engine's channel default).
# Scan the channel's recent videos and skip any already produced (POSTED, QUEUE, or
# PENDING_VERIFY) -- the channel's #1 video doesn't change daily, and always grabbing it
# re-clips the same source repeatedly (duplicate-content/shadowban risk on the posting
# account). PENDING_VERIFY must be included: an unverified post may still resolve to
# published by the self-heal reconciler, so it counts as "already produced" too.
if [ -z "$URL" ]; then
  while IFS= read -r CAND; do
    [ -n "$CAND" ] || continue
    if [ ! -f "$POSTED/${CAND}_EN.mp4" ] && [ ! -f "$QUEUE/${CAND}_EN.mp4" ] && [ ! -f "$PENDING_VERIFY/${CAND}_EN.mp4" ]; then
      VID="$CAND"; break
    fi
  done < <("$PY" -m yt_dlp --flat-playlist --playlist-end 150 --print "%(id)s" \
        "https://www.youtube.com/@theDiaryOfACEO/videos" 2>/dev/null)
  [ -n "${VID:-}" ] && URL="https://www.youtube.com/watch?v=$VID"
fi
[ -n "$URL" ] || { emit "no source url"; exit 0; }

WORK="/tmp/clip-producer-$$"; mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

# 1) source → clip → 9:16 (pipeline.py: yt-dlp 1080p, whisper, Gemini rank, crop)
if ! "$PY" "$SKILLS/pipeline.py" --url "$URL" --out "$WORK" --lang en --count 1 >"$WORK/pipe.log" 2>&1; then
  emit "pipeline failed for $URL (see /tmp log)"; exit 0
fi
# pipeline.py outputs clip.crop.mp4 (uncaptioned 9:16) + clip.final.mp4 (its own srt burn).
# We want OUR karaoke captions, so burn_captions runs on the UNCAPTIONED crop.
CLIP="$WORK/clip.crop.mp4"; [ -f "$CLIP" ] || CLIP="$WORK/clip.final.mp4"
[ -f "$CLIP" ] || CLIP=$(ls "$WORK"/*.mp4 2>/dev/null | grep -v raw | head -1)
[ -f "$CLIP" ] || { emit "no clip produced"; exit 0; }

# 2) burn EN karaoke captions (hook = first transcript line, optional)
HOOK=$("$PY" -c "import json;s=json.load(open('$WORK/transcript.json'));print((s[0].get('text','') if s else '')[:60])" 2>/dev/null || echo "")
"$PY" "$SKILLS/burn_captions.py" "$CLIP" "$WORK/EN.mp4" --hook "$HOOK" --model tiny --lang en >>"$WORK/pipe.log" 2>&1

# 3) verify GATE (9:16 / audio / non-black / 8-90s / resolution+bitrate floor)
if ! bash "$SKILLS/verify_clip.sh" "$WORK/EN.mp4" >>"$WORK/pipe.log" 2>&1; then
  emit "clip FAILED verify_clip — not queued (source $URL)"
  # Lessons wiring (2026-07-10 quality self-heal): a gate rejection is a mechanical fact
  # (measured WxH/bitrate), not a judgment call, so this one row is appended
  # deterministically -- the loop's own STARTUP prompt already reads this file every pass.
  GW=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$WORK/EN.mp4" 2>/dev/null | head -1)
  GH=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$WORK/EN.mp4" 2>/dev/null | head -1)
  GVBR=$(ffprobe -v error -select_streams v:0 -show_entries stream=bit_rate -of csv=p=0 "$WORK/EN.mp4" 2>/dev/null | head -1)
  mkdir -p "$HOME/.cloak"
  python3 - "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${PASS_ID:-clip-producer-$(date -u +%Y-%m-%dT%H:%M:%SZ)}" \
    "${GW:-0}" "${GH:-0}" "${GVBR:-0}" "$HOME/.cloak/clip-lessons.jsonl" <<'PY'
import json, sys
ts, pass_id, gw, gh, gvbr, path = sys.argv[1:7]
row = {
    "ts": ts, "pass_id": pass_id,
    "mistake": f"render below quality floor {gw}x{gh},{gvbr}",
    "lesson": "check format selector / source quality",
    "issue_type": "quality_gate_reject", "severity": "major",
}
with open(path, "a") as f:
    f.write(json.dumps(row, ensure_ascii=False) + "\n")
PY
  exit 0
fi

# 4) queue it (deterministic id from source) + caption sidecar
ID=$(printf '%s' "$URL" | sed 's#.*v=##; s#[^A-Za-z0-9_-]##g' | head -c 16)
DEST="$QUEUE/${ID}_EN.mp4"
cp "$WORK/EN.mp4" "$DEST"
# MON-5: if an affiliate/product offer is set, add a "link in bio" CTA (IG only makes the BIO link
# clickable, never caption links). The bio link itself is set on the account by the AFF-FIND step.
CTA="Follow for daily money + AI clips."
if [ -s "$HOME/clips/offer.json" ]; then
  OFFER=$(python3 -c "import json;d=json.load(open('$HOME/clips/offer.json'));print(d.get('offer_name','') if d.get('joined') else '')" 2>/dev/null)
  [ -n "$OFFER" ] && CTA="👉 ${OFFER} — link in bio. Follow for daily money + AI."
fi
cat > "$QUEUE/${ID}_EN.txt" <<CAP
${HOOK:-Money & AI clip} 💰

${CTA}

#money #investing #ai #wealth #finance #aiclips
CAP
emit "queued $(basename "$DEST") (verified, from $URL)"
