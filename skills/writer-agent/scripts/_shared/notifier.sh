#!/usr/bin/env bash
# notifier.sh — env-driven notification for the article loop (spec #70, OSS self-containment).
#
# Delegates to Life Manager's shared Telegram client. No env vars set = silent no-op (never
# blocks or crashes the caller; the loop must run with zero notification configured).
#
# Setup (put in ~/.local/state/life-manager/.env):
#   TELEGRAM_BOT_TOKEN=<token from @BotFather>
#   TELEGRAM_CHAT_ID=<your chat id, e.g. from @userinfobot>
#
# Usage:
#   bash notifier.sh "message text"        # standalone CLI
#   source notifier.sh; notify "message text"   # sourced, as a function
#
# Exit code: 0 on successful send OR on a deliberate no-op (env vars unset -- that is not
# a failure, it is the documented default). Non-zero only when the env vars ARE set but
# the actual Telegram API call failed (network error, bad token, etc.) -- callers that
# care can check this; callers that do not (most) can ignore it, matching the old
# telegram_notify()'s `return $?` contract.

notify() {
  local text="$1"
  local script_dir repo_root sender env_file
  env_file="${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"
  if [ -f "$env_file" ]; then
    set -a; . "$env_file" 2>/dev/null || true; set +a
  fi
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
      echo "[notifier] Telegram configuration not set -- no-op (this is the documented default, not an error)" >&2
      return 0
    fi
  fi
  script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  repo_root="$(cd "$script_dir/../../../.." && pwd)"
  sender="$repo_root/skills/_shared/send-telegram.sh"
  "$sender" "$text" "${TELEGRAM_CHAT_ID:-$TELEGRAM_ALERT_CHAT_ID}" >/dev/null
}

# Only run as a standalone CLI when NOT sourced (mirrors the `source ...; notify ...`
# usage documented above -- sourcing must not also execute a stray notify call).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  if [ $# -lt 1 ]; then
    echo "usage: notifier.sh \"message text\"" >&2
    exit 2
  fi
  notify "$1"
  exit $?
fi
