#!/usr/bin/env bash
set -uo pipefail
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

TO="${1:-}"
SUBJECT="${2:-}"
TEXT="${3:-}"
INBOX="${4:-${AGENTMAIL_INBOX_ID:-}}"
[ -n "$TO" ] && [ -n "$SUBJECT" ] && [ -n "$TEXT" ] && [ -n "$INBOX" ] || exit 2
[ -n "${AGENTMAIL_API_KEY:-}" ] || exit 2

STATE_ROOT="${AGENTMAIL_STATE_ROOT:-$HOME/.local/state/life-manager/agentmail}"
STATE_DIR="${AGENTMAIL_ADAPTER_STATE_DIR:-$STATE_ROOT/state/adapter}"
mkdir -p "$STATE_DIR"
LOG="$STATE_DIR/sent-log.jsonl"
touch "$LOG"; chmod 600 "$LOG"
NOW=$(date -u +%s)
RECENT=$(awk -v cutoff=$((NOW - 3600)) -F'"ts_unix":' '/sent/ { ts=$2+0; if (ts>=cutoff) c++ } END { print c+0 }' "$LOG")
[ "$RECENT" -lt 60 ] || { echo "[agentmail/send] rate cap hit ($RECENT/h)" >&2; exit 75; }

PAYLOAD=$(jq -nc --arg to "$TO" --arg subj "$SUBJECT" --arg text "$TEXT" \
  '{to:[$to],subject:$subj,text:$text}')
RESP=$(curl -sS -X POST "https://api.agentmail.to/v0/inboxes/$INBOX/messages/send" \
  -H "Authorization: Bearer $AGENTMAIL_API_KEY" -H "Content-Type: application/json" \
  -d "$PAYLOAD" -w "\n%{http_code}")
HTTP_CODE=$(printf '%s\n' "$RESP" | tail -1)
BODY=$(printf '%s\n' "$RESP" | sed '$d')
MSG_ID=$(printf '%s' "$BODY" | jq -r '.message_id // .id // empty' 2>/dev/null)
STATUS=sent; [ "$HTTP_CODE" -lt 300 ] || STATUS=error
jq -nc --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson ts_unix "$NOW" \
  --arg to "$TO" --arg subj "$SUBJECT" --argjson http "$HTTP_CODE" --arg msg_id "$MSG_ID" \
  --arg status "$STATUS" '{ts:$ts,ts_unix:$ts_unix,to:$to,subject:$subj,http:$http,message_id:$msg_id,status:$status}' >>"$LOG"
echo "[agentmail/send] http=$HTTP_CODE status=$STATUS message_id=$MSG_ID"
[ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]
