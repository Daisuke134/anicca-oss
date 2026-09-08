#!/bin/bash
# article-healthcheck.sh — 5-min launchd poller (spec #22 self-heal L2).
#
# Detects "the daily article pass did not start" or "it started but never finished" faster
# than a human would notice, and alerts by Telegram. That is the WHOLE job -- see the design
# note (task #22 report, 2026-07-16) for why the other three observed failure classes
# (Substack/X session expiry, zenn slug) are deliberately NOT re-checked here: they are
# already detected once per day by article-daily.sh's own pass (STEP 6-9), and the real
# blocker for those is a human re-login (2FA), which polling every 5 minutes cannot fix --
# "monitoring only belongs on what someone can act on; high-frequency re-checking of a fact
# nobody can change is anxiety, not monitoring."
#
# Deliberately does NOT touch the shared daily-driver browser -- ensure_browser.sh already
# runs once per day, inside article-daily.sh's own lock, right before that pass needs it;
# touching the browser here too, every 5 minutes, independent of that lock, would race it.
#
# Deliberately does NOT source or exec any part of article-daily.sh. Running fragments of
# that script (to read a variable, etc.) re-triggers its own `echo ... >>"$LOG"` startup line
# with no corresponding pass -- this is what produced three phantom "run" log entries with no
# "done" on 2026-07-16 07:08 (confirmed root cause: team-lead sourcing script fragments to
# extract PROMPT for inspection). This script only ever READS the log as plain text.
set -u
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/writer-runtime-env.sh
source "$SCRIPT_DIR/scripts/writer-runtime-env.sh"

LOG="${ARTICLE_DAILY_LOG:-$WRITER_LOG_DIR/article-daily.log}"     # override for testing
STATE_DIR="${ARTICLE_HEALTHCHECK_STATE_DIR:-$WRITER_STATE_DIR}"    # override for testing
mkdir -p "$STATE_DIR"
. "$LIFE_MANAGER_REPO/skills/_shared/scripts/telegram-notify.sh" 2>/dev/null || exit 0

TODAY="$(TZ=Asia/Tokyo date +%F)"
NOW_HHMM="${ARTICLE_HEALTHCHECK_NOW_HHMM:-$(TZ=Asia/Tokyo date +%H%M)}"
ALERT_STAMP="$STATE_DIR/.article-healthcheck-alerted-$TODAY"
LAUNCHCTL="${ARTICLE_LAUNCHCTL:-launchctl}"
LAUNCHD_DOMAIN="${ARTICLE_LAUNCHD_DOMAIN:-gui/$(id -u)}"

[ -f "$LOG" ] || exit 0          # fresh install, nothing to check yet
[ "$NOW_HHMM" -lt "0605" ] && exit 0   # before today's scheduled 06:00 fire (+5min grace)
"$LAUNCHCTL" print "$LAUNCHD_DOMAIN/ai.anicca.article-daily" >/dev/null 2>&1 || exit 0
[ -f "$ALERT_STAMP" ] && exit 0        # already alerted today -- do not spam every 5 min

RUN_LN="$(grep -n "=== article-daily run $TODAY" "$LOG" | tail -1 | cut -d: -f1)"
if [ -z "${RUN_LN:-}" ]; then
  telegram_notify "article-healthcheck: no article-daily run recorded for $TODAY yet (past 06:05 JST). launchd may not have fired, or the loop crashed before its first log line." \
    && touch "$ALERT_STAMP"
  exit 0
fi

DONE_LN="$(grep -n "=== article-daily done" "$LOG" | tail -1 | cut -d: -f1)"
if [ -n "${DONE_LN:-}" ] && [ "$DONE_LN" -gt "$RUN_LN" ]; then
  exit 0   # today's run already finished (rc may still be non-zero -- article-daily.sh
           # itself alerts on that; this poller's job is only "did it finish at all")
fi

RUN_LINE="$(sed -n "${RUN_LN}p" "$LOG")"
RUN_TS="$(printf '%s' "$RUN_LINE" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}')"
RUN_EPOCH="$(TZ=Asia/Tokyo date -j -f "%Y-%m-%d %H:%M:%S" "$RUN_TS" "+%s" 2>/dev/null || echo 0)"
NOW_EPOCH="$(date +%s)"
[ "$RUN_EPOCH" -le 0 ] && exit 0   # could not parse the timestamp -- do not guess, skip silently
AGE_MIN=$(( (NOW_EPOCH - RUN_EPOCH) / 60 ))

if [ "$AGE_MIN" -gt 90 ]; then
  telegram_notify "article-healthcheck: today's article-daily pass started ${AGE_MIN} min ago ($RUN_TS JST) and has not finished (no matching done line in the log yet). May be stuck." \
    && touch "$ALERT_STAMP"
fi
exit 0
