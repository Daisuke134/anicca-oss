#!/usr/bin/env bash
set -euo pipefail

PHOTO_PATH="${1:?usage: send-telegram-photo.sh <photo-path> <caption> [chat_id]}"
CAPTION="${2:?caption is required}"
[ -f "$PHOTO_PATH" ] || { echo "TELEGRAM_PHOTO_SENT=false ERROR=photo_missing"; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
if [ -n "${3:-}" ]; then
  RESPONSE="$("${PYTHON:-python3}" "$SCRIPT_DIR/telegram.py" --chat-id "$3" photo "$PHOTO_PATH" --caption "$CAPTION")"
else
  RESPONSE="$("${PYTHON:-python3}" "$SCRIPT_DIR/telegram.py" photo "$PHOTO_PATH" --caption "$CAPTION")"
fi
MSG_ID="$(printf '%s' "$RESPONSE" | "${PYTHON:-python3}" -c 'import json,sys
receipt=json.load(sys.stdin)
values=receipt.get("message_ids", [])
value=values[-1] if isinstance(values, list) and values else None
valid=receipt.get("status") == "delivered" and receipt.get("method") == "sendPhoto" and type(value) is int and value > 0
print(value if valid else "")')"
[ -n "$MSG_ID" ] || { echo "TELEGRAM_PHOTO_SENT=false"; exit 1; }
echo "TELEGRAM_PHOTO_SENT=true MSGID=$MSG_ID"
