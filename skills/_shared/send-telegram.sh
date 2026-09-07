#!/usr/bin/env bash
# send-telegram.sh — generic Telegram report sender for ANY loop (clip, gig, video, etc).
# The LLM composes the natural-language summary; this tool only does the deterministic send
# (per feedback_build_agents_not_hardcode_regex: model does judgment, tool does the mechanical part).
#
#   bash send-telegram.sh "<message text>" [chat_id]
#
# Thin compatibility wrapper around the one shared Python Telegram client.
set -euo pipefail
MSG="${1:?usage: send-telegram.sh \"<message>\" [chat_id]}"

LIFE_MANAGER_STATE_HOME="${LIFE_MANAGER_STATE_HOME:-$HOME/.local/state/life-manager}"
export LIFE_MANAGER_ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$LIFE_MANAGER_STATE_HOME/.env}"
[ -f "$LIFE_MANAGER_ENV_FILE" ] && set -a && source "$LIFE_MANAGER_ENV_FILE" && set +a
CHAT_ID="${2:-${TELEGRAM_ALERT_CHAT_ID:?chat_id argument or TELEGRAM_ALERT_CHAT_ID is required}}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
"${PYTHON:-python3}" "$SCRIPT_DIR/telegram.py" --chat-id "$CHAT_ID" text "$MSG" >/dev/null
echo "TELEGRAM_SENT=true"
