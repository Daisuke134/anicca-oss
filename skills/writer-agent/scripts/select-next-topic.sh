#!/usr/bin/env bash
# select-next-topic.sh — deterministic queue-card picker for article-daily.sh STEP 1.
#
# Why this exists: on 2026-07-17 a batch of 9 topic cards was created by
# a retired batch generator with identical filesystem mtimes (same second). STEP 1's
# prose instruction ("take the OLDEST one") had no deterministic tiebreak, so the
# agent fell back to `ls` default (alphabetical) ordering -- a card starting with
# a digit ("2026-07-17-devlog.md") was picked ahead of an actually-older card
# ("virtuals-acp.md") purely because '2' < 'c' in ASCII. This is mechanical, not
# a judgment call, so it must never depend on agent interpretation again.
#
# Sort key: created (frontmatter, ISO8601) ASC -> priority (frontmatter, int,
# lower = first; absent = treated as tied, falls through to filename) ASC ->
# filename ASC. A card missing `created:` falls back to its file mtime (with a
# warning to stderr) so old/hand-written cards without the field still work.
#
# Usage: select-next-topic.sh <queue_dir> [--exclude-basename <card.md>]...
# --exclude-basename は同一 pass 内だけ対象 card を除外する。翌日の通常実行には影響しない。
# stdout: the full path of the winning card, or nothing if the queue is empty.
# exit: 0 if a card was picked, 1 if the queue is empty or all cards are excluded.
set -uo pipefail

QUEUE_DIR="${1:?usage: select-next-topic.sh <queue_dir> [--exclude-basename <card.md>]...}"
shift
EXCLUDED_BASENAMES=()
while [ $# -gt 0 ]; do case "$1" in
  --exclude-basename)
    [ $# -ge 2 ] || { echo "FATAL: --exclude-basename requires a filename" >&2; exit 2; }
    EXCLUDED_BASENAMES+=("$2")
    shift 2
    ;;
  *) echo "FATAL: unknown arg: $1" >&2; exit 2;;
esac; done

# A pre-publication resume has one immutable claimed card.  Prefer that exact
# basename while it is still in queue; once the model moves it to in-progress,
# or explicitly excludes it after a conscience block, fall back to normal order
# so an alternate card can be selected in the same pass.
RESUME_CARD_BASENAME="${ARTICLE_RESUME_CARD_BASENAME:-}"
if [ -n "$RESUME_CARD_BASENAME" ]; then
  RESUME_EXCLUDED=0
  for excluded in "${EXCLUDED_BASENAMES[@]-}"; do
    [ "$excluded" = "$RESUME_CARD_BASENAME" ] && RESUME_EXCLUDED=1
  done
  RESUME_CARD_PATH="$QUEUE_DIR/$RESUME_CARD_BASENAME"
  RESUME_CARD_LEAF="${RESUME_CARD_PATH##*/}"
  if [ "$RESUME_EXCLUDED" -eq 0 ] \
    && [ "$RESUME_CARD_LEAF" = "$RESUME_CARD_BASENAME" ] \
    && [ -f "$RESUME_CARD_PATH" ] && [ ! -L "$RESUME_CARD_PATH" ]; then
    printf '%s\n' "$RESUME_CARD_PATH"
    exit 0
  fi
fi

shopt -s nullglob
cards=("$QUEUE_DIR"/*.md)
shopt -u nullglob

if [ "${#cards[@]}" -eq 0 ]; then
  exit 1
fi

# macOS 標準 Bash 3.2 は空配列の ${array[@]} を nounset で unbound 扱いする。
# 引数展開中だけ nounset を外し、selector 本体の exit code はそのまま返す。
set +u
python3 - "$QUEUE_DIR" "${#EXCLUDED_BASENAMES[@]}" "${EXCLUDED_BASENAMES[@]}" "${cards[@]}" <<'PYEOF'
import sys, os, re, datetime

queue_dir = sys.argv[1]
exclude_count = int(sys.argv[2])
excluded_basenames = set(sys.argv[3:3 + exclude_count])
cards = [path for path in sys.argv[3 + exclude_count:] if os.path.basename(path) not in excluded_basenames]
if not cards:
    raise SystemExit(1)

def read_frontmatter(path):
    created = None
    priority = None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    m = re.match(r"^---\n(.*?\n)---\n", text, re.S)
    fm = m.group(1) if m else ""
    cm = re.search(r'^created:\s*["\']?([^"\'\n]+)["\']?\s*$', fm, re.M)
    if cm:
        raw = cm.group(1).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                s = raw.replace("Z", "+00:00") if fmt.endswith("Z") else raw
                created = datetime.datetime.strptime(s, fmt.replace("Z", "+00:00") if fmt.endswith("Z") else fmt)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=datetime.timezone.utc)
                break
            except ValueError:
                continue
    pm = re.search(r'^priority:\s*(-?\d+)\s*$', fm, re.M)
    if pm:
        priority = int(pm.group(1))
    return created, priority

FAR_FUTURE_PRIORITY = 10**9

rows = []
for path in cards:
    created, priority = read_frontmatter(path)
    if created is None:
        # fallback: file mtime, so old/hand-written cards without `created:` still work
        try:
            created = datetime.datetime.fromtimestamp(os.path.getmtime(path), tz=datetime.timezone.utc)
        except OSError:
            created = datetime.datetime.now(tz=datetime.timezone.utc)
        print(f"WARNING: {path} has no created: frontmatter, falling back to mtime", file=sys.stderr)
    if priority is None:
        priority = FAR_FUTURE_PRIORITY
    rows.append((created, priority, os.path.basename(path), path))

rows.sort(key=lambda r: (r[0], r[1], r[2]))
print(rows[0][3])
PYEOF
rc=$?
set -u
exit "$rc"
