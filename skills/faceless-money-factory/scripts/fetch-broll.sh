#!/usr/bin/env bash
# fetch-broll.sh <query> <out_dir> [n] — get N faceless finance b-roll clips ($0, no API key).
# Tries Mixkit (free, commercial-OK, no login) for fresh clips matching the topic; ALWAYS
# falls back to the local broll-library so the daily run can NEVER fail on footage. New fetched
# clips are also cached into the library so it grows over time (more visual variety each day).
set -uo pipefail
QUERY="${1:-money}"; OUT="${2:?out_dir required}"; N="${3:-8}"
SK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LIB="${FACELESS_VIDEO_BROLL_ROOT:-$HOME/.local/state/life-manager/faceless-money-factory/broll-library}"
mkdir -p "$OUT" "$LIB"

# 1) try Mixkit fresh fetch (query + finance fallback categories), 1080p, with hard timeouts
urls=""
for q in "$QUERY" money business finance investment; do
  u="$(curl -sL --max-time 12 -A "Mozilla/5.0" "https://mixkit.co/free-stock-video/$q/" 2>/dev/null \
      | grep -oE 'https://assets\.mixkit\.co/videos/[0-9]+/[0-9]+-1080\.mp4' | sort -u | head -3)"
  urls="$urls"$'\n'"$u"
done
urls="$(printf '%s\n' "$urls" | sed '/^$/d' | sort -u | head -$((N+2)))"

i=0
while IFS= read -r url; do
  [ -z "$url" ] && continue
  id="$(printf '%s' "$url" | grep -oE '[0-9]+-1080' | head -1)"
  dst="$LIB/mx_${id}.mp4"
  if [ ! -s "$dst" ]; then
    curl -sL --max-time 45 -A "Mozilla/5.0" "$url" -o "$dst" 2>/dev/null || rm -f "$dst"
  fi
  [ -s "$dst" ] || continue
  i=$((i+1))
done <<< "$urls"

# 2) fill OUT from the library (fetched + seeded), up to N, shuffled for variety
shopt -s nullglob
pool=( "$LIB"/*.mp4 )
[ ${#pool[@]} -gt 0 ] || { echo "NO_BROLL (library empty and fetch failed)" >&2; exit 4; }
# shuffle
idxs=$(printf '%s\n' "${!pool[@]}" | awk 'BEGIN{srand('"$(date +%s)"')}{print rand()"\t"$0}' | sort | cut -f2)
c=0
for k in $idxs; do
  [ $c -ge "$N" ] && break
  c=$((c+1))
  cp "${pool[$k]}" "$OUT/b$c.mp4"
done
echo "BROLL_READY n=$c (fetched_new=$i, library=${#pool[@]}) in $OUT"
