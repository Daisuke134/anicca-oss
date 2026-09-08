#!/usr/bin/env bash
# topic-supply.sh — count what is left to write, before the morning it runs out.
#
# The loop has watched its outlet for months: exact8, receipts, ledgers, all
# instrumented. Nobody counted the inlet. Measured 2026-07-27 05:40, fifteen
# minutes before a scheduled publish: one card in the runtime queue, which that
# run would consume, and every raw idea already published. The hardest rule in
# this system is that a day is never skipped, and its largest threat turned out
# to be neither a gate nor a judge but an empty shelf.
#
# Read-only by design. It counts and warns; refilling is a separate decision,
# because a malformed card breaks topic selection outright and that failure is
# worse than the shortage it was meant to cure.
#
#   topic-supply.sh [--min N] [--quiet]
#   exit 0 = at or above the floor, 1 = below it
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SKILL_DIR="${ARTICLE_SKILL_DIR:-${ARTICLE_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd -P)}}"
QUEUE_DIR="${ARTICLE_TOPIC_QUEUE:-$SKILL_DIR/state/topics/queue}"
IDEAS_DIR="${ARTICLE_RAW_IDEAS:-$SKILL_DIR/state/raw-ideas}"
# Three days of head room: enough to notice on a Friday and act by Monday.
MIN=3
QUIET=0

while [ $# -gt 0 ]; do case "$1" in
  --min)   MIN="${2:-3}"; shift 2 ;;
  --quiet) QUIET=1; shift ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac; done

QUEUE=0
[ -d "$QUEUE_DIR" ] && QUEUE=$(find "$QUEUE_DIR" -maxdepth 1 -name '*.md' -type f 2>/dev/null | wc -l | tr -d ' ')

# A raw idea only counts when it is genuinely writable: status ready AND not
# the README, whose schema line literally reads "status: ready | drafting |
# ..." and inflated this count to one when the real answer was zero.
READY=0
if [ -d "$IDEAS_DIR" ]; then
  while IFS= read -r idea; do
    case "$(basename "$idea")" in README.md) continue ;; esac
    grep -qE '^status:[[:space:]]*ready[[:space:]]*$' "$idea" 2>/dev/null && READY=$((READY + 1))
  done < <(find "$IDEAS_DIR" -maxdepth 1 -name '*.md' -type f 2>/dev/null)
fi

TOTAL=$((QUEUE + READY))
printf '{"queue":%s,"raw_ideas_ready":%s,"total":%s,"floor":%s,"ok":%s}\n' \
  "$QUEUE" "$READY" "$TOTAL" "$MIN" "$([ "$TOTAL" -ge "$MIN" ] && echo true || echo false)"

[ "$TOTAL" -ge "$MIN" ] && exit 0

if [ "$QUIET" = "0" ]; then
  MESSAGE="topic supply low: $TOTAL left (queue $QUEUE, ready ideas $READY), floor $MIN. The daily publish runs out of material, not out of quality."
  SENDER="$REPO_ROOT/skills/_shared/send-telegram.sh"
  if [ -n "${ARTICLE_TELEGRAM_TARGET:-}" ]; then
    "$SENDER" "$MESSAGE" "$ARTICLE_TELEGRAM_TARGET" >/dev/null 2>&1 || true
  else
    "$SENDER" "$MESSAGE" >/dev/null 2>&1 || true
  fi
fi
echo "topic-supply: $TOTAL below floor $MIN -- the daily publish will run dry" >&2
exit 1
