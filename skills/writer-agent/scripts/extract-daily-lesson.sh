#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ARTICLE_ROOT="${ARTICLE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh" || exit $?
T=$(TZ=Asia/Tokyo date +%Y-%m-%d)
EXP="${WRITER_EXPERIENCE_DIR:-$WRITER_STATE_DIR/experience-log}/$T.jsonl"
STATE_DIR="$WRITER_STATE_DIR"
OUT="$STATE_DIR/daily-lesson-$T.md"
if [ ! -s "$EXP" ]; then
  echo "EMPTY experience-log: $EXP" >&2
  exit 2
fi
{
  echo "# Anicca Daily Lessons $T"; echo ""
  echo "## Cron self-heals"; jq -r 'select(.kind=="cron_fix") | "- \(.target): \(.payload)"' "$EXP" 2>/dev/null | head -10
  echo "## Money moves"; jq -r 'select(.kind=="earn") | "- \(.target): \(.payload)"' "$EXP" 2>/dev/null | head -5
} > "$OUT"
echo "$OUT"
