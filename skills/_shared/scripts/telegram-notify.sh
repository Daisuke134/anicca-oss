#!/usr/bin/env bash
# telegram-notify.sh — direct-from-bash Telegram alert via Life Manager's shared sender.
# For launchd / out-of-band scripts that do not have a channel delivery field.
#
# Usage (source then call):
#   source "$LIFE_MANAGER_REPO/skills/_shared/scripts/telegram-notify.sh"
#   telegram_notify "text here"

# An alert must never be able to kill its caller. `${VAR:?msg}` exits a non-interactive shell
# outright -- `|| true` at the call site does not catch a parameter-expansion error -- so a missing
# chat id took down the whole tick at exactly the moment it had something to report. Measured
# 2026-09-07: session_vault_tick died on this line every 30 minutes for hours, right after
# "ALERT: session dead for: https://coconala.com/mypage/dashboard", so everything after that point,
# including the session re-banking that would have healed it, never ran. Load the id from the same
# env file the rest of the fleet uses, and fail soft with a stderr line.
telegram_notify() {
  local text="$1"
  local target="${TELEGRAM_ALERT_CHAT_ID:-}"
  local script_dir sender
  if [ -z "$target" ]; then
    set -a; . "$HOME/.local/state/life-manager/.env" 2>/dev/null || true; set +a
    target="${TELEGRAM_ALERT_CHAT_ID:-}"
  fi
  if [ -z "$target" ]; then
    echo "telegram_notify: no TELEGRAM_ALERT_CHAT_ID, dropping alert: $text" >&2
    return 1
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  sender="$script_dir/../send-telegram.sh"
  [ -x "$sender" ] || {
    echo "telegram_notify: shared sender is unavailable: $sender" >&2
    return 1
  }
  "$sender" "$text" "$target" >/dev/null 2>&1
  return $?
}
