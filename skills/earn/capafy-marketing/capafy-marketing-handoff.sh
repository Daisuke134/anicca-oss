#!/usr/bin/env bash
# Deterministic terminal classifier for the Capafy Instagram marketing pass.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
set -uo pipefail
RUNNER_RC="${1:-1}"; EVIDENCE_DIR="${2:-none}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${CAPAFY_OUTCOME_STATE_DIR:-$HOME/.local/state/life-manager/state}"
RESULT="${CAPAFY_MARKETING_RESULT:-$STATE/capafy-marketing-result.json}"
OUTCOME="${CAPAFY_OUTCOME_SCRIPT:-$HERE/scripts/capafy_outcome.py}"
SENDER="${CAPAFY_TELEGRAM_SENDER:-$HERE/../../_shared/send-telegram.sh}"
LIFECYCLE="${CAPAFY_IG_LIFECYCLE:-$HERE/scripts/capafy_ig_lifecycle.py}"
ACCOUNTS="${CAPAFY_IG_ACCOUNTS_FILE:-$HOME/.cloak/clip-accounts-capafy.json}"
LIFECYCLE_STATE="${CAPAFY_IG_LIFECYCLE_STATE:-$STATE/capafy-ig-lifecycle.json}"
KICKSTART="${CAPAFY_LAUNCHCTL:-launchctl}"
EVENT_ADAPTER="${CAPAFY_EVENT_ADAPTER:-$HERE/scripts/capafy_event_adapters.py}"
EVENT_LEDGER="${CAPAFY_EVENT_LEDGER:-$STATE/capafy-revenue-events.jsonl}"
EVENT_EVIDENCE_DIR="${CAPAFY_EVENT_EVIDENCE_DIR:-$STATE/capafy-revenue-evidence}"
TERMINAL="$STATE/capafy-marketing-terminal.json"
mkdir -p "$STATE"

field(){ python3 - "$RESULT" "$1" <<'PY'
import json,sys
try: value=json.load(open(sys.argv[1])).get(sys.argv[2])
except Exception: value=None
print("" if value is None else value)
PY
}
send_receipt(){
  local output response id rc
  output="$(mktemp -t capafy-telegram.XXXXXX)" || return 1
  bash "$SENDER" "$1" >"$output" 2>&1; rc=$?
  if [ "$rc" -eq 0 ] && [ "$(awk 'END {print NR}' "$output")" = 1 ]; then response="$(cat "$output")"; fi
  rm -f "$output"
  [[ "${response:-}" =~ ^TELEGRAM_SENT=true\ MSGID=([0-9]+)$ ]] || return 1
  id="${BASH_REMATCH[1]}"; printf '%s' "$id"
}
record_terminal(){
  python3 - "$TERMINAL" "$1" "$2" <<'PY'
import datetime,json,os,sys
path,msg,envelope=sys.argv[1:4]; tmp=path+".tmp"
data={"schema_version":1,"telegram_message_id":msg,"recorded_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"outcome":json.loads(envelope)}
with open(tmp,"w") as f: json.dump(data,f,ensure_ascii=False,indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
os.replace(tmp,path)
PY
}
future_retry(){
  python3 - "$@" <<'PY'
import datetime,sys
now=datetime.datetime.now(datetime.timezone.utc)
latest=now+datetime.timedelta(hours=1)
for value in sys.argv[1:]:
    try:
        parsed=datetime.datetime.fromisoformat(value.replace("Z","+00:00"))
    except (AttributeError,ValueError):
        continue
    if parsed.tzinfo is not None and now<parsed.astimezone(datetime.timezone.utc)<=latest:
        print(parsed.astimezone(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")); raise SystemExit
print((now+datetime.timedelta(minutes=5)).isoformat(timespec="seconds").replace("+00:00","Z"))
PY
}
recovery_row_state(){
  python3 - "$ACCOUNTS" "$1" <<'PY'
import json,sys
rows=json.load(open(sys.argv[1])); handle=sys.argv[2].lstrip("@").lower()
matches=[row for row in rows if isinstance(row,dict) and str(row.get("handle") or "").lstrip("@").lower()==handle]
if len(matches)!=1: raise SystemExit(2)
print(matches[0].get("status") or "")
PY
}
replacement_matches(){
  python3 - "$LIFECYCLE_STATE" "$1" <<'PY'
import json,sys
try: state=json.load(open(sys.argv[1]))
except Exception: state={}
print(str(state.get("replacement_requested") is True and state.get("incident_id")==sys.argv[2]).lower())
PY
}
incident(){
  local reason="$1" lifecycle="$2" retry="$3" replace_handle="${4:-}" recovery="${5:-false}" fingerprint raw id payload body msg existing_key existing_retry delivery_key row_state
  fingerprint="$(printf '%s' "$reason"|shasum -a 256|awk '{print $1}')"
  raw="$(python3 "$OUTCOME" start-incident --owner marketer --summary "$reason" --fingerprint "$fingerprint")" || return 2
  id="$(python3 -c 'import json,sys;print(json.load(sys.stdin)["incident_id"])' <<<"$raw")"
  existing_key="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("terminal_message_key") or "")' <<<"$raw")"
  existing_retry="$(python3 -c 'import json,sys;print(json.load(sys.stdin).get("next_retry_at") or "")' <<<"$raw")"
  retry="$(future_retry "$existing_retry" "$retry")" || return 2
  if [ "$recovery" = true ] && [ -n "$replace_handle" ]; then
    row_state="$(recovery_row_state "$replace_handle")" || return 2
    if [ "$row_state" != session_failed ]; then
      python3 "$LIFECYCLE" retire --accounts "$ACCOUNTS" --handle "$replace_handle" \
        --reason "$reason" --incident-id "$id" >/dev/null || return 2
    fi
    if [ "$(replacement_matches "$id")" != true ]; then
      python3 "$LIFECYCLE" request-replacement --state "$LIFECYCLE_STATE" \
        --handle "$replace_handle" --reason "$reason" --incident-id "$id" >/dev/null || return 2
      "$KICKSTART" kickstart "gui/$(id -u)/ai.anicca.capafy-ig-account-manager" >/dev/null 2>&1 || return 2
    fi
  elif [ -n "$replace_handle" ]; then
    python3 "$LIFECYCLE" retire --accounts "$ACCOUNTS" --handle "$replace_handle" \
      --reason "$reason" --incident-id "$id" >/dev/null 2>&1 || true
    python3 "$LIFECYCLE" request-replacement --state "$LIFECYCLE_STATE" \
      --handle "$replace_handle" --reason "$reason" --incident-id "$id" >/dev/null || return 2
    "$KICKSTART" kickstart "gui/$(id -u)/ai.anicca.capafy-ig-account-manager" >/dev/null 2>&1 || true
  fi
  payload="$(python3 - "$id" "$reason" "$lifecycle" "$retry" <<'PY'
import json,sys
print(json.dumps({"incident_id":sys.argv[1],"phase":"unresolved","repair_summary":sys.argv[3],"next_retry_at":sys.argv[4]}))
PY
)"
  printf '%s' "$payload"|python3 "$OUTCOME" transition-incident >/dev/null || return 2
  [ -z "$existing_key" ] || return 1
  delivery_key="$(printf 'capafy-marketing-direct-failure:%s:%s' "$id" "$reason"|shasum -a 256|awk '{print $1}')"
  payload="$(python3 - "$id" "$delivery_key" <<'PY'
import json,sys
print(json.dumps({"incident_id":sys.argv[1],"phase":"unresolved","terminal_message_key":sys.argv[2]}))
PY
)"
  printf '%s' "$payload"|python3 "$OUTCOME" transition-incident >/dev/null || return 2
  body="Capafy Marketer incident $id: $reason. $lifecycle. Next automatic action: $retry. Evidence: $EVIDENCE_DIR. Human action required: none."
  msg="$(send_receipt "$body")" || return 1
  payload="$(python3 - "$id" "$msg" <<'PY'
import json,sys
print(json.dumps({"incident_id":sys.argv[1],"phase":"unresolved","telegram_message_id":int(sys.argv[2])}))
PY
)"
  printf '%s' "$payload"|python3 "$OUTCOME" transition-incident >/dev/null || return 2
  return 1
}

[ -f "$RESULT" ] || { incident "marketing runner produced no deterministic terminal artifact (rc=$RUNNER_RC)" "The technical repair owner will inspect the runner boundary" "the next repair cycle"; exit $?; }
KIND="$(field result)"
if [ "$KIND" = "challenge" ] || [ "$KIND" = "replacement_waiting" ]; then
  reason="$(field reason)"; retry="$(field next_retry_at)"; [ -n "$retry" ]||retry="the next account lifecycle pass"
  handle="$(field handle)"
  recovery="$(field session_recovery)"
  if [ "$recovery" = "True" ] || [ "$recovery" = "true" ]; then
    recovery=true
    reason="active Instagram browser tab is missing"
    summary="$reason"
    lifecycle="$(field repair_detail)"; [ -n "$lifecycle" ] || lifecycle="The browser owner proof failed closed and the existing replacement-account workflow is active"
  elif [ "$KIND" = "challenge" ]; then
    recovery=false
    summary="Instagram platform challenge on @$handle: $reason"
    lifecycle="Retries are contained and the replacement-account workflow is active"
  else
    recovery=false
    summary="Instagram account verification failed for @$handle: $reason"
    lifecycle="Retries are contained and the replacement-account workflow is active"
  fi
  incident "$summary" "$lifecycle" "$retry" "$handle" "$recovery"; exit $?
fi
if [ "$RUNNER_RC" -ne 0 ] || [ "$KIND" = "failure" ]; then
  reason="$(field reason)"; [ -n "$reason" ]||reason="marketing runner exited rc=$RUNNER_RC"
  incident "$reason" "The technical repair owner is active; this is not evidence of an account ban" "the next repair cycle"; exit $?
fi

case "$KIND" in
  account_created)
    ENVELOPE="$(cat "$RESULT"|python3 -c 'import json,sys;d=json.load(sys.stdin);d["schema_version"]=1;d["kind"]="account_created";d["owner"]="marketer";d.pop("result",None);print(json.dumps(d))')" ;;
  scheduled)
    ENVELOPE="$(python3 - "$(field handle)" <<'PY'
import json,sys
print(json.dumps({"schema_version":1,"kind":"account_state","owner":"marketer","handle":sys.argv[1] or "unknown","scheduler_loaded":True,"lifecycle_status":"unknown","capability":"none","session_established":False,"public_post_url":None}))
PY
)" ;;
  dry)
    ENVELOPE="$(cat "$RESULT"|python3 -c 'import json,sys;d=json.load(sys.stdin);d["schema_version"]=1;d["kind"]="marketing_dry";d["owner"]="marketer";d.pop("result",None);print(json.dumps(d))')" ;;
  published)
    ENVELOPE="$(cat "$RESULT"|python3 -c 'import json,sys;d=json.load(sys.stdin);d["schema_version"]=1;d["kind"]="marketing_published";d["owner"]="marketer";d.pop("result",None);print(json.dumps(d))')" ;;
  *) incident "unsupported marketing terminal result=$KIND" "The technical repair owner will repair the outcome contract" "the next repair cycle"; exit $? ;;
esac

if [ "$KIND" = published ]; then
  ENVELOPE="$(python3 - "$ENVELOPE" "$LIFECYCLE_STATE" <<'PY'
import json,sys
envelope=json.loads(sys.argv[1])
if not envelope.get("handle"):
    try: envelope["handle"]=json.load(open(sys.argv[2])).get("handle")
    except Exception: envelope["handle"]=None
print(json.dumps(envelope))
PY
)" || exit 2
fi

MEDIA_PATH="$(python3 - "$ENVELOPE" <<'PY'
import json,sys
print(json.loads(sys.argv[1]).get("media_path") or "")
PY
)"
if [ -n "$MEDIA_PATH" ] && [ ! -f "$MEDIA_PATH" ]; then
  incident "marketing media artifact is missing: $MEDIA_PATH" "The content pass cannot claim completion without its real media" "the next repair cycle"; exit $?
fi
BODY="$(printf '%s' "$ENVELOPE"|python3 "$OUTCOME" render)" || { incident "marketing terminal outcome failed validation" "Missing evidence prevents a publish claim" "the next repair cycle"; exit $?; }
printf '%s' "$ENVELOPE" | python3 "$EVENT_ADAPTER" append-outcome \
  --outcome-stdin --source "$RESULT" --ledger "$EVENT_LEDGER" \
  --evidence-dir "$EVENT_EVIDENCE_DIR" --technical-evidence-dir "$EVIDENCE_DIR" \
  >/dev/null || exit 2
if [ -f "$TERMINAL" ] && python3 - "$TERMINAL" "$ENVELOPE" <<'PY'
import json,sys
try:
 old=json.load(open(sys.argv[1])); new=json.loads(sys.argv[2]); msg=str(old.get("telegram_message_id") or "")
 raise SystemExit(0 if msg.isdigit() and old.get("outcome")==new else 1)
except Exception: raise SystemExit(1)
PY
then
  exit 0
fi
MSG_ID="$(send_receipt "$BODY")" || exit 2
record_terminal "$MSG_ID" "$ENVELOPE"
exit 0
