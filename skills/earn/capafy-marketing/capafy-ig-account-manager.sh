#!/usr/bin/env bash
# Immediate, bounded replacement-account owner for the Capafy Instagram lifecycle.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:${PATH:-}"
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
if [ "${CAPAFY_IG_ACCOUNT_MANAGER_PROBE_ONLY:-0}" = "1" ]; then
  printf 'task_class=marketing-agent interval=300 terminal_owner=capafy-marketing-handoff.sh\n'
  exit 0
fi

export ANICCA_BUDGET_SCOPE_ID="${CAPAFY_MARKETING_PASS_ID:-$(date +%s)-$$}"
export ANICCA_PASS_TOKEN_BUDGET="${CAPAFY_MARKETING_PASS_TOKEN_BUDGET:-1048576}"
export ANICCA_LOOP_DAILY_TOKEN_BUDGET="${CAPAFY_MARKETING_DAILY_TOKEN_BUDGET:-2097152}"
export ANICCA_BUDGET_DAILY_SCOPE="${CAPAFY_MARKETING_BUDGET_DAILY_SCOPE:-capafy-ig-marketing-daily}"
export ANICCA_TOKEN_BUDGET_LEDGER="${CAPAFY_TOKEN_BUDGET_LEDGER:-$HOME/.local/state/anicca/telemetry/token-budget.jsonl}"

ENGINE="$HERE/../marketing-engine"
# shellcheck source=../marketing-engine/provision_prompt.sh
. "$ENGINE/provision_prompt.sh"
LIFECYCLE="${CAPAFY_IG_LIFECYCLE:-$HERE/scripts/capafy_ig_lifecycle.py}"
ACCOUNTS="${CAPAFY_IG_ACCOUNTS_FILE:-$HOME/.cloak/clip-accounts-capafy.json}"
STATE_DIR="${CAPAFY_OUTCOME_STATE_DIR:-$HOME/.local/state/life-manager/state}"
STATE="${CAPAFY_IG_LIFECYCLE_STATE:-$STATE_DIR/capafy-ig-lifecycle.json}"
RESULT="${CAPAFY_MARKETING_RESULT:-$STATE_DIR/capafy-account-manager-result.json}"
HANDOFF="${CAPAFY_MARKETING_HANDOFF:-$HERE/capafy-marketing-handoff.sh}"
RUN_AGENT="${CAPAFY_RUN_AGENT:-$ENGINE/run_agent.sh}"
BROWSER="${CAPAFY_PROVISION_BROWSER:-$HERE/../../browser/ensure_provision_browser.sh}"
GUARD="${CAPAFY_BROWSER_GUARD:-$REPO_ROOT/skills/browser/browser-guard.sh}"
VERIFY_SESSION="${CAPAFY_IG_SESSION_VERIFY:-$HERE/scripts/capafy_ig_session_verify.py}"
KICKSTART="${CAPAFY_LAUNCHCTL:-launchctl}"
LOCK="${CAPAFY_ACCOUNT_MANAGER_LOCK_DIR:-$STATE_DIR/capafy-ig-account-manager.lock}"
IDENTITY="${CAPAFY_PROVISION_BROWSER_IDENTITY:-instagram:capafy-provision}"
mkdir -p "$STATE_DIR" "$(dirname "$ACCOUNTS")"
[ -f "$ACCOUNTS" ] || printf '[]\n' >"$ACCOUNTS"
write_result(){
  python3 - "$RESULT" "$1" "$2" "${3:-}" <<'PY'
import json,os,sys
p,kind,reason,handle=sys.argv[1:5]; value={"result":kind,"reason":reason}
if handle: value.update(handle=handle,next_retry_at="immediately")
t=p+".tmp"
with open(t,"w") as f: json.dump(value,f); f.write("\n"); f.flush(); os.fsync(f.fileno())
os.replace(t,p)
PY
}
write_session_recovery_result(){
  python3 - "$RESULT" "$1" <<'PY'
import datetime,json,os,sys
path,handle=sys.argv[1:]
retry=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=5)
value={
    "result":"replacement_waiting",
    "session_recovery":True,
    "reason":"active Instagram browser tab is missing",
    "handle":handle,
    "repair_detail":"The existing browser-owned recovery row is retained for structural reauthentication.",
    "next_retry_at":retry.isoformat(timespec="seconds").replace("+00:00","Z"),
}
tmp=path+".tmp"
with open(tmp,"w") as stream:
    json.dump(value,stream); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
os.replace(tmp,path)
PY
}
resolve_session_recovery_handle(){
  python3 - "$ACCOUNTS" "$STATE" <<'PY'
import json,re,sys
accounts_path,state_path=sys.argv[1:]
state=json.load(open(state_path))
incident_id=state.get("incident_id")
if not incident_id:
    raise SystemExit(1)
rows=json.load(open(accounts_path))
matches=[
    row for row in rows
    if isinstance(row,dict)
    and row.get("status")=="session_failed"
    and row.get("session_owner")=="browser"
    and row.get("incident_id")==incident_id
]
if len(matches)!=1:
    raise SystemExit(1)
handle=str(matches[0].get("handle") or "").lstrip("@").lower()
if not re.fullmatch(r"capafy\.[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?", handle):
    raise SystemExit(1)
print(handle)
PY
}
session_recovery_shortcut(){
  local handle="$1"
  write_session_recovery_result "$handle"
  exit 1
}
attempt_existing_session_recovery(){
  local cdp port handle verified matches=""
  cdp="$(AI_BROWSER_HOLDER_PID=$$ AI_BROWSER_GUARD="$GUARD" bash "$BROWSER" "$IDENTITY")" || return 1
  browser_leased=1
  port="${cdp##*:}"
  case "$port" in ''|*[!0-9]*) return 1;; esac
  while IFS= read -r handle; do
    [ -n "$handle" ] || continue
    if { [ -x "$VERIFY_SESSION" ] && "$VERIFY_SESSION" --accounts "$ACCOUNTS" --handle "$handle" --port "$port" --current-session; } >/dev/null 2>&1 \
      || { [ ! -x "$VERIFY_SESSION" ] && python3 "$VERIFY_SESSION" --accounts "$ACCOUNTS" --handle "$handle" --port "$port" --current-session; } >/dev/null 2>&1; then
      matches="${matches}${matches:+ }${handle}"
    fi
  done < <(python3 - "$ACCOUNTS" <<'PY'
import json,re,sys
rows=json.load(open(sys.argv[1]))
seen=set()
for row in rows if isinstance(rows,list) else []:
    handle=str(row.get("handle") or "").lstrip("@").lower() if isinstance(row,dict) else ""
    if row.get("session_owner")=="browser" and re.fullmatch(r"capafy\.[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?",handle) and handle not in seen:
        seen.add(handle); print(handle)
PY
)
  [ -n "$matches" ] && [ "${matches#* }" = "$matches" ] || return 1
  verified="$matches"
  python3 - "$ACCOUNTS" "$verified" "$port" "$IDENTITY" <<'PY' || return 1
import datetime,json,os,sys
path,handle,port,identity=sys.argv[1:]
rows=json.load(open(path)); found=0
for row in rows:
    if isinstance(row,dict) and str(row.get("handle") or "").lstrip("@").lower()==handle:
        row.update(status="ready_browser",port=int(port),session_owner="browser",browser_identity=identity,session_recovered_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z"))
        for key in ("poisoned","poisoned_at","frozen_at","blocked_at","retired_at","retirement_reason","incident_id","failure_reason","replacement_requested"):
            row.pop(key,None)
        found+=1
if found!=1: raise SystemExit(2)
tmp=path+".tmp"
with open(tmp,"w") as stream: json.dump(rows,stream,indent=2); stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
os.replace(tmp,path)
PY
  python3 "$LIFECYCLE" snapshot --accounts "$ACCOUNTS" --state "$STATE" >/dev/null || return 1
  rm -f "$RESULT"
  if [ "${CAPAFY_RECOVERY_SKIP_PUBLISHER_KICK:-0}" != "1" ]; then
    "$KICKSTART" kickstart -k "gui/$(id -u)/ai.anicca.capafy-ig-marketing-daily" >/dev/null 2>&1 || true
  fi
  return 0
}
fail(){
  local reason="$1" rc
  write_result failure "$reason"
  CAPAFY_MARKETING_RESULT="$RESULT" CAPAFY_IG_LIFECYCLE_STATE="$STATE" bash "$HANDOFF" 1 account-manager
  rc=$?
  exit "$rc"
}
replacement_fail(){
  local reason="$1" handle="$2" rc
  write_result replacement_waiting "$reason" "$handle"
  CAPAFY_MARKETING_RESULT="$RESULT" CAPAFY_IG_LIFECYCLE_STATE="$STATE" bash "$HANDOFF" 1 account-manager
  rc=$?
  exit "$rc"
}

if ! mkdir "$LOCK" 2>/dev/null; then
  old_pid="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && ! kill -0 "$old_pid" 2>/dev/null; then
    rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null || exit 0
    mkdir "$LOCK" 2>/dev/null || exit 0
  else
    exit 0
  fi
fi
printf '%s\n' "$$" >"$LOCK/pid"
browser_leased=0
cleanup(){
  if [ "$browser_leased" = "1" ]; then
    bash "$GUARD" release "$IDENTITY" >/dev/null 2>&1 || true
  fi
  rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null || true
}
trap cleanup EXIT

# A failed sender leaves the verified result for this pass to retry without
# provisioning a second account.
if [ -f "$RESULT" ] && [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("result", ""))' "$RESULT" 2>/dev/null)" = "account_created" ]; then
  CAPAFY_MARKETING_RESULT="$RESULT" CAPAFY_IG_LIFECYCLE_STATE="$STATE" bash "$HANDOFF" 0 account-manager || exit $?
  rm -f "$RESULT"
  "$KICKSTART" kickstart -k "gui/$(id -u)/ai.anicca.capafy-ig-marketing-daily" >/dev/null 2>&1 || true
  exit 0
fi

python3 "$LIFECYCLE" snapshot --accounts "$ACCOUNTS" --state "$STATE" >/dev/null || fail "could not derive the Instagram lifecycle snapshot"
replacement="$(python3 -c 'import json,sys;print(str(json.load(open(sys.argv[1])).get("replacement_requested",False)).lower())' "$STATE")"
[ "$replacement" = "true" ] || exit 0
prior_result_kind="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("result", ""))' "$RESULT" 2>/dev/null || true)"

recovery_handle="$(resolve_session_recovery_handle 2>/dev/null || true)"
if [ -n "$recovery_handle" ] && [ "$prior_result_kind" != "challenge" ]; then
  attempt_existing_session_recovery && exit 0
  session_recovery_shortcut "$recovery_handle"
fi

python3 - "$STATE" <<'PY' || fail "could not persist the provisioning lifecycle state"
import datetime,json,os,sys
p=sys.argv[1]; d=json.load(open(p)); d.update(status="provisioning",updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z")); t=p+".tmp"
with open(t,"w") as f: json.dump(d,f,indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
os.replace(t,p)
PY
before_count="$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))))' "$ACCOUNTS")" || fail "account registry is not a valid JSON list"
cdp="$(AI_BROWSER_HOLDER_PID=$$ AI_BROWSER_GUARD="$GUARD" bash "$BROWSER" "$IDENTITY")" || fail "isolated provisioning browser did not start"
browser_leased=1
port="${cdp##*:}"
case "$port" in ''|*[!0-9]*) fail "isolated provisioning browser returned no numeric port";; esac
context="capafy-account-manager-$$"
prompt="$(IG_PROVISION_ACCOUNT_STATE_FILE="$ACCOUNTS" IG_PROVISION_HANDLE_PREFIX=capafy.skills IG_PROVISION_INSTANCE=capafy IG_PROVISION_GMAIL_PLUS_TAG_PREFIX=capafy IG_PROVISION_BIO_TEXT='AI skills that solve recurring work, no link' IG_PROVISION_BROWSER_INSTRUCTIONS="Attach only to $cdp for $IDENTITY." IG_PROVISION_BROWSER_IDENTITY="$IDENTITY" IG_PROVISION_PROFILE_PREFIX=capafy-mkt IG_PROVISION_REASON=replacement IG_PROVISION_PORT="$port" IG_PROVISION_CONTEXT_ID="$context" IG_PROVISION_TELEGRAM_TARGET= render_ig_provision_prompt)" || fail "account provisioning prompt failed isolation validation"
prompt="$prompt
The shell caller exclusively owns Telegram and lifecycle reporting. Do not send messages and do not attempt any password, private-API, Client().login, or login_by_sessionid recovery."
evidence="$STATE_DIR/agent-runner-evidence/capafy-account-manager/$(date +%s)-$$"
printf '%s\n' "$prompt" | "$RUN_AGENT" --task-class marketing-agent --evidence-dir "$evidence" --task-label capafy-ig-account-manager --loop capafy >/dev/null 2>&1 || fail "account provisioning agent did not complete"

candidate="$(python3 - "$ACCOUNTS" "$before_count" <<'PY'
import json,sys
rows=json.load(open(sys.argv[1])); before=int(sys.argv[2])
print(rows[-1].get("handle", "") if len(rows)==before+1 and isinstance(rows[-1],dict) else "")
PY
)" || candidate=""
readback="$(python3 - "$ACCOUNTS" "$before_count" "$port" "$IDENTITY" <<'PY'
import json,sys
rows=json.load(open(sys.argv[1])); before=int(sys.argv[2]); port=int(sys.argv[3]); identity=sys.argv[4]
if len(rows)!=before+1: raise SystemExit(2)
r=rows[-1]
required=(r.get("handle") and r.get("status")=="warming" and r.get("session_owner")=="browser" and int(r.get("port") or 0)==port and r.get("context_id") and r.get("browser_identity")==identity)
if not required: raise SystemExit(2)
print(r["handle"])
PY
)" || { [ -n "$candidate" ] && replacement_fail "provisioning appended a malformed account row" "$candidate"; fail "provisioning did not append exactly one identifiable account row"; }
credential="$HOME/.cloak/ig-$readback.json"
[ -f "$credential" ] || replacement_fail "new account credential file is missing" "$readback"
if [ -x "$VERIFY_SESSION" ]; then
  "$VERIFY_SESSION" --accounts "$ACCOUNTS" --credential "$credential" --handle "$readback" --port "$port" >/dev/null || replacement_fail "isolated browser session verification failed" "$readback"
else
  python3 "$VERIFY_SESSION" --accounts "$ACCOUNTS" --credential "$credential" --handle "$readback" --port "$port" >/dev/null || replacement_fail "isolated browser session verification failed" "$readback"
fi

python3 - "$STATE" "$readback" <<'PY' || replacement_fail "verified session state could not be persisted" "$readback"
import datetime,json,os,sys
p,h=sys.argv[1:3]; d=json.load(open(p)); d.update(schema_version=1,status="publish_probe_ready",handle=h,session_owner="browser",session_established=True,capability="publish_probe",last_public_reel_url=None,post_write_session_verified=False,reach_healthy=False,replacement_requested=False,updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z")); d.pop("warmup_success_dates",None); d.pop("warmup_successes",None); t=p+".tmp"
with open(t,"w") as f: json.dump(d,f,indent=2); f.write("\n"); f.flush(); os.fsync(f.fileno())
os.replace(t,p)
PY
python3 - "$RESULT" "$readback" <<'PY' || replacement_fail "verified account terminal could not be persisted" "$readback"
import json,os,sys
p,h=sys.argv[1:3]; t=p+".tmp"; value={"result":"account_created","handle":h,"session_owner":"browser","session_established":True,"capability":"publish_probe","public_post_url":None,"next_action":"publish and verify the first original product-education Reel"}
with open(t,"w") as f: json.dump(value,f); f.write("\n"); f.flush(); os.fsync(f.fileno())
os.replace(t,p)
PY
CAPAFY_MARKETING_RESULT="$RESULT" CAPAFY_IG_LIFECYCLE_STATE="$STATE" bash "$HANDOFF" 0 "$evidence" || exit $?
rm -f "$RESULT"
"$KICKSTART" kickstart -k "gui/$(id -u)/ai.anicca.capafy-ig-marketing-daily" >/dev/null 2>&1 || true
exit 0
