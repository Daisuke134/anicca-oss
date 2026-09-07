#!/usr/bin/env bash
# x-repost-healthcheck.sh — the loop is a launchd one-shot, so there is no tmux session to keep
# alive. What can silently die here is the SCHEDULE and the SESSION, so those are what this checks:
#   1. the pass plist is still bootstrapped (a corrupt/unloaded plist = a loop that never fires)
#   2. a pass actually reached a decision recently (state/.last-pass heartbeat)
# It alerts rather than restarting: re-running a pass out of band would risk a second post in a day.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "${LIFE_MANAGER_LOOP_ID:-}" = "x-repost-ja-healthcheck" ]; then
  DEFAULT_LOOP_NAME="x-repost-ja"
  DEFAULT_LABEL="ai.anicca.x-repost-ja-pass"
  DEFAULT_STATE="$HOME/loops/x-repost-ja"
  DEFAULT_MAX_AGE_SECONDS=5400
  DEFAULT_INITIAL_GRACE_SECONDS=3600
else
  DEFAULT_LOOP_NAME="x-repost"
  DEFAULT_LABEL="ai.anicca.x-repost-pass"
  DEFAULT_STATE="$HOME/loops/x-repost-en"
  DEFAULT_MAX_AGE_SECONDS=10800
  DEFAULT_INITIAL_GRACE_SECONDS=0
fi
LOOP_NAME="${X_LOOP_NAME:-$DEFAULT_LOOP_NAME}"
LABEL="${X_LOOP_LABEL:-$DEFAULT_LABEL}"
INSTALLED="$HOME/Library/LaunchAgents/$LABEL.plist"
HEARTBEAT="${X_REPOST_STATE_DIR:-$DEFAULT_STATE}/.last-pass"
# Hourly cadence, so 3h of silence is already three missed passes -- and the heartbeat is written
# on every pass that reaches a decision, not only the ones that publish.
MAX_AGE_SECONDS="${X_REPOST_MAX_PASS_AGE:-$DEFAULT_MAX_AGE_SECONDS}"
INITIAL_GRACE_SECONDS="${X_LOOP_INITIAL_GRACE_SECONDS:-$DEFAULT_INITIAL_GRACE_SECONDS}"

# shellcheck source=/dev/null
source "$HOME/.openclaw/skills/_shared/scripts/telegram-notify.sh" 2>/dev/null || \
  telegram_notify() { echo "telegram_notify unavailable: $1" >&2; }

problems=()

# 1. schedule present and loadable
if [ ! -f "$INSTALLED" ]; then
  problems+=("plist is missing from LaunchAgents")
elif ! plutil -lint "$INSTALLED" >/dev/null 2>&1; then
  problems+=("installed plist is corrupt")
elif ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  problems+=("$LABEL is not loaded")
fi

# 2. a pass reached a decision recently
if [ ! -f "$HEARTBEAT" ]; then
  installed_age=$(( $(date +%s) - $(stat -f %m "$INSTALLED" 2>/dev/null || echo 0) ))
  if [ "$INITIAL_GRACE_SECONDS" -le 0 ] || [ "$installed_age" -gt "$INITIAL_GRACE_SECONDS" ]; then
    problems+=("no pass has ever completed (state/.last-pass absent)")
  fi
else
  age=$(( $(date +%s) - $(stat -f %m "$HEARTBEAT") ))
  if [ "$age" -gt "$MAX_AGE_SECONDS" ]; then
    problems+=("last completed pass was $((age / 3600))h ago (limit $((MAX_AGE_SECONDS / 3600))h)")
  fi
fi

if [ "${#problems[@]}" -eq 0 ]; then
  echo "$LOOP_NAME: OK"
  exit 0
fi

printf '%s: %s\n' "$LOOP_NAME" "${problems[@]}" >&2
telegram_notify "$LOOP_NAME::: healthcheck — $(printf '%s; ' "${problems[@]}")"
exit 1
