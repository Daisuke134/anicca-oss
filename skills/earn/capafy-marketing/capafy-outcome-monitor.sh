#!/usr/bin/env bash
# Deliver a Capafy self-fix terminal outcome once, only after deterministic validation.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${CAPAFY_OUTCOME_STATE_DIR:-$HOME/.local/state/life-manager/state}"
OUTCOME="${CAPAFY_OUTCOME_SCRIPT:-$SCRIPT_DIR/scripts/capafy_outcome.py}"
SENDER="${CAPAFY_TELEGRAM_SENDER:-$SCRIPT_DIR/../../_shared/send-telegram.sh}"
SIDECAR="$STATE/.self-fix-capafy-loop.incident.json"
LOCK="$STATE/.capafy-outcome-monitor.lock"
mkdir -p "$STATE"

if ! mkdir "$LOCK" 2>/dev/null; then
  lock_mtime="$(stat -f %m "$LOCK" 2>/dev/null || echo 0)"
  if [ $(( $(date +%s) - lock_mtime )) -gt 600 ]; then
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

[ -f "$SIDECAR" ] || exit 0
INCIDENT_ID="$(python3 - "$SIDECAR" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1]))["incident_id"])
except Exception:
    raise SystemExit(2)
PY
)" || exit 2
RESULT_PATH="$(python3 - "$SIDECAR" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1]))["result_path"])
except Exception:
    raise SystemExit(2)
PY
)" || exit 2
[ -f "$RESULT_PATH" ] || exit 0

RESULT_LINE="$(head -1 "$RESULT_PATH" 2>/dev/null)"
RESULT_STATUS="${RESULT_LINE%% *}"
case "$RESULT_STATUS" in
  RUNNING|'') exit 0 ;;
  SUCCESS|FAIL) ;;
  *) exit 0 ;;
esac

RECORD="$(python3 "$OUTCOME" get-incident --incident-id "$INCIDENT_ID")" || exit 2
CURRENT_PHASE="$(python3 - "$RECORD" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["phase"])
PY
)" || exit 2
# A verified incident is terminal even when an old self-fix sidecar still points
# at a code-only SUCCESS marker.  The verified business evidence is authoritative;
# never rebuild an unresolved envelope or resend Telegram from stale repair state.
[ "$CURRENT_PHASE" = "verified" ] && exit 0
ENVELOPE="$(python3 - "$RECORD" "$RESULT_STATUS" "$RESULT_LINE" <<'PY'
import json, sys
record = json.loads(sys.argv[1])
status, result_line = sys.argv[2:4]
outcome = record.get("outcome")
if status == "SUCCESS" and isinstance(outcome, dict):
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0)
detail = result_line.split(" ", 2)[2] if len(result_line.split(" ", 2)) == 3 else result_line
if status == "SUCCESS":
    repair = "The self-fixer completed its code work, but the original business observable was not attached."
    blocker = "The business outcome is not verified; code completion alone cannot close this incident."
else:
    repair = record.get("repair_summary") or "The autonomous repair ran and returned a terminal failure."
    blocker = detail or record.get("summary") or "The blocker remains unresolved."
print(json.dumps({
    "schema_version": 1,
    "kind": "incident_unresolved",
    "incident_id": record["incident_id"],
    "owner": record["owner"],
    "detected_summary": record.get("summary") or "A Capafy operation failed.",
    "repair_summary": repair,
    "blocker": blocker,
    "next_retry_at": record.get("next_retry_at") or "the next scheduled repair cycle",
}, ensure_ascii=False))
PY
)" || exit 2

BODY="$(printf '%s' "$ENVELOPE" | python3 "$OUTCOME" render)" || exit 2
KEY="$(printf '%s' "$ENVELOPE" | python3 "$OUTCOME" delivery-key)" || exit 2
CURRENT_KEY="$(python3 - "$RECORD" <<'PY'
import json, sys
print(json.loads(sys.argv[1]).get("terminal_message_key") or "")
PY
)"

KIND="$(python3 - "$ENVELOPE" <<'PY'
import json, sys
print(json.loads(sys.argv[1])["kind"])
PY
)"
transition() {
  local phase="$1" extra="${2:-}"
  [ -n "$extra" ] || extra='{}'
  python3 - "$INCIDENT_ID" "$phase" "$extra" <<'PY' | python3 "$OUTCOME" transition-incident >/dev/null
import json, sys
payload = json.loads(sys.argv[3])
payload.update({"incident_id": sys.argv[1], "phase": sys.argv[2]})
print(json.dumps(payload))
PY
}

send_receipt() {
  local output response rc
  output="$(mktemp -t capafy-outcome-telegram.XXXXXX)" || return 1
  bash "$SENDER" "$1" >"$output" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ] || [ "$(awk 'END {print NR}' "$output")" != 1 ]; then
    rm -f "$output"
    return 1
  fi
  response="$(cat "$output")"
  rm -f "$output"
  if [[ "$response" =~ ^TELEGRAM_SENT=true\ MSGID=([0-9]+)$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}
reservation_payload() {
  python3 - "$1" <<'PY'
import json,sys
print(json.dumps({"terminal_message_key":sys.argv[1]}))
PY
}
complete_closure() {
  local delivery_status="$1" message_id="${2:-}" verification
  case "$CURRENT_PHASE" in
    detected) transition repair_started || return 1; transition repaired || return 1 ;;
    repair_started) transition repaired || return 1 ;;
    unresolved) transition repair_started || return 1; transition repaired || return 1 ;;
    repaired) ;;
    verified) return 0 ;;
    *) return 1 ;;
  esac
  if [ "$delivery_status" = confirmed ]; then
    verification="$(printf '{"business_outcome_validated":true,"telegram_delivery_status":"confirmed","telegram_message_id":%s}' "$message_id")"
    transition verified "$(printf '{"terminal_message_key":"%s","telegram_message_id":%s,"verification":%s}' "$KEY" "$message_id" "$verification")"
  else
    verification='{"business_outcome_validated":true,"telegram_delivery_status":"reserved_unconfirmed"}'
    transition verified "$(printf '{"terminal_message_key":"%s","verification":%s}' "$KEY" "$verification")"
  fi
}

if [ "$KIND" = "repair_closure" ]; then
  if [ "$CURRENT_KEY" = "$KEY" ]; then
    exit 0
  fi
  transition "$CURRENT_PHASE" "$(reservation_payload "$KEY")" || exit 2
else
  [ -z "$CURRENT_KEY" ] || exit 0
  transition unresolved "$(reservation_payload "$KEY")" || exit 2
fi

if [ "$KIND" = "repair_closure" ]; then
  MESSAGE_ID="$(send_receipt "$BODY")"
  send_rc=$?
  if [ "$send_rc" -eq 0 ]; then
    complete_closure confirmed "$MESSAGE_ID" || exit 2
    exit 0
  fi
  complete_closure reserved_unconfirmed || exit 2
  exit 1
else
  MESSAGE_ID="$(send_receipt "$BODY")" || exit 1
  transition unresolved "$(printf '{"terminal_message_key":"%s","telegram_message_id":"%s"}' "$KEY" "$MESSAGE_ID")" || exit 2
fi

exit 0
