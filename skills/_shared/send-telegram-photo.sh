#!/usr/bin/env bash
set -euo pipefail

PHOTO_PATH="${1:?usage: send-telegram-photo.sh <photo-path> <caption> [chat_id]}"
CAPTION="${2:?caption is required}"
[ -f "$PHOTO_PATH" ] || { echo "TELEGRAM_PHOTO_SENT=false ERROR=photo_missing"; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
ARGS=()
if [ -n "${3:-}" ]; then
  ARGS+=(--chat-id "$3")
fi
RESPONSE="$("${PYTHON:-python3}" "$SCRIPT_DIR/telegram.py" "${ARGS[@]}" photo "$PHOTO_PATH" --caption "$CAPTION")"
MSG_ID="$(printf '%s' "$RESPONSE" | "${PYTHON:-python3}" -c 'import json,sys
value=json.load(sys.stdin).get("message_ids", [])
print(value[-1] if isinstance(value, list) and value else "")')"
[ -n "$MSG_ID" ] || { echo "TELEGRAM_PHOTO_SENT=false"; exit 1; }
echo "TELEGRAM_PHOTO_SENT=true MSGID=$MSG_ID"
