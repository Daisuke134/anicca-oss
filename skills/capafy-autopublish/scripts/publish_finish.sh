#!/usr/bin/env bash
# publish_finish.sh — DETERMINISTIC second half of a Capafy publish, run AFTER the
# agent has driven CP1 (Agent-Card save) to is_confirmed_skills=true.
#   verify-CP1 -> prepare -> continue_upload -> CP2(key host) -> CP3(submit) ->
#   verify -> ledger
# Fail-closed: refuses to start unless is_confirmed_skills=true; exits 0 only if the final
# remote-status is platform_status=1 (under review) AND is_confirmed_config_keys=true.
#
# Usage: publish_finish.sh <agent-id> <skill-name> [LISTING.md]
set -euo pipefail

ID="${1:?agent-id required}"
SKILL_NAME="${2:?skill-name required}"
LISTING="${3:-}"

AUTO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PUB="$AUTO/vendor/capafy-publisher"
LIFE_MANAGER_STATE_HOME="${LIFE_MANAGER_STATE_HOME:-$HOME/.local/state/life-manager}"
CAPAFY_PUBLISH_HOME="${CAPAFY_PUBLISH_HOME:-$LIFE_MANAGER_STATE_HOME/runtime/capafy-publisher-home}"
CAPAFY_PUBLISHER_STATE_HOME="${CAPAFY_PUBLISHER_STATE_HOME:-$LIFE_MANAGER_STATE_HOME/runtime/capafy-publisher}"
VENV="${CAPAFY_BROWSER_PYTHON:-python3}"

# Keep package state bound to the selected agent even when a previous retry
# left a recoverable manifest behind.  The publisher reads this before parsing
# its command, so it must be exported before the first Python invocation.
# The selected remote agent is the isolation boundary.  Do not preserve an
# inherited work directory: a launcher can carry one over from a different
# candidate, making submission silently operate on that other manifest.
export CAPAFY_PUBLISH_WORK_DIR="$CAPAFY_PUBLISHER_STATE_HOME/work/agents/$ID"

# Direct recovery and launchd must resolve credentials from the same repo-external
# SSOT. Load them before the key-health gate; values stay process-local.
for ENV_FILE in "$LIFE_MANAGER_STATE_HOME/.env"; do
  if [ -f "$ENV_FILE" ]; then
    set -a; . "$ENV_FILE" 2>/dev/null; set +a
  fi
done

export HOME="$CAPAFY_PUBLISH_HOME"
export CAPAFY_PUBLISHER_STATE_HOME
cd "$PUB" || { echo "❌ cd PUB"; exit 1; }

step(){ echo ""; echo "━━━ $* ━━━"; }
die(){ echo "❌ $*"; exit 1; }

# strict=False: remote-status JSON embeds raw newlines in the description field
# ("Invalid control character" would otherwise crash json.load -> empty gate -> false die).
rstat(){ local field="$1"; python3 packager.py publish-remote-status --agent-id "$ID" 2>/dev/null | python3 -c "import json,sys
try:
 value=json.loads(sys.stdin.read(),strict=False).get('latest_version',{}).get('$field')
 if isinstance(value,bool): print('1' if value else '0')
 elif value is None: print('')
 else: print(str(value))
except Exception: print('')"; }
refresh(){ python3 packager.py publish-refresh-url --agent-id "$ID" --step "$1" 2>/dev/null | EXPECTED_AGENT_ID="$ID" python3 -c "import json,os,sys
try:
 payload=json.loads(sys.stdin.read(),strict=False)
 if str(payload.get('agent_id','') or '').strip() != os.environ.get('EXPECTED_AGENT_ID',''):
  raise ValueError('agent_id mismatch')
 url=str(payload.get('review_url','') or '').strip()
 if not url: raise ValueError('missing review_url')
 print(url)
except Exception: print('')"; }
# poll a remote-status field until it reaches <want> (server is eventually-consistent:
# a browser save lands a few seconds AFTER the toast/URL flips, so a ONE-SHOT read of a
# stale value is a false negative — that race is what stalled the loop). $2=field $3=want.
poll(){ local field="$1" want="$2" tries="${3:-15}" slp="${4:-5}" i v
  for ((i=0;i<tries;i++)); do v="$(rstat "$field")"; [ "$v" = "$want" ] && { echo "$field=$v (${i}x)"; return 0; }; sleep "$slp"; done
  echo "$field=$v (want $want, gave up ${tries}x${slp}s)"; return 1; }

INITIAL_PLATFORM_STATUS="$(rstat platform_status)"
case "$INITIAL_PLATFORM_STATUS" in
  0)
    # 2026-07-18 A1: fail-closed key-health gate. A submit into an under-funded
    # OpenRouter account triggered billing-error review rejections.
    "$AUTO/scripts/key_health_gate.sh" 5.00 || die "KEY-HEALTH gate FAIL — top up OpenRouter (>= \$5 remaining) before submitting; see state/lessons.md"
    ;;
  1)
    [ "$(rstat is_confirmed_skills)" = "1" ] || die "platform_status=1 but is_confirmed_skills is not confirmed; verify only"
    [ "$(rstat is_confirmed_config_keys)" = "1" ] || die "platform_status=1 but is_confirmed_config_keys is not confirmed; verify only"
    [ "$(rstat package_uploaded)" = "1" ] || die "platform_status=1 but package_uploaded is not true; verify only"
    echo "platform_status=1 with skills/config confirmed ✓ — idempotent verify/skip"
    ;;
  2)
    die "platform_status=2 review_rejected — rerun publish_prepare/publish-init for a new version under the same Agent ID; no submit retry"
    ;;
  *)
    die "unsupported or unreadable initial platform_status=${INITIAL_PLATFORM_STATUS:-empty}; no external effect"
    ;;
esac

if [ "$INITIAL_PLATFORM_STATUS" = "0" ]; then
  step "[2b] verify CP1 (fail-closed, polled)"
  # Short poll (not one-shot): the agentic CP1 save may still be registering server-side.
  poll is_confirmed_skills 1 6 5 || die "CP1 not confirmed (is_confirmed_skills!=1) — drive CP1 agentically first (CP1_AGENTIC.md)"
  echo "is_confirmed_skills=1 ✓"

  PUBLISH_REVIEW_URL=""

# prepare/upload + CP2 are idempotent-skippable only after both the config and
# package-upload readbacks are true. If upload is needed, continue_upload is
# intentionally invoked exactly once: a timeout or persistence error is never an
  # automatic upload retry.
  CONFIG_STATE="$(rstat is_confirmed_config_keys)"
  case "$CONFIG_STATE" in
    0|1) ;;
    *) die "is_confirmed_config_keys readback is missing or unknown; no external effect" ;;
  esac
  if [ "$CONFIG_STATE" = "1" ]; then
    [ "$(rstat package_uploaded)" = "1" ] || die "is_confirmed_config_keys=1 but package_uploaded is not true; no upload retry"
    echo "is_confirmed_config_keys=1 and package_uploaded=1 already ✓ — skip prepare/upload+CP2"
  else
    case "$(rstat package_uploaded)" in
      0)
        step "[3] publish-submit prepare"
        if [ -e "$CAPAFY_PUBLISH_WORK_DIR/staging" ]; then
          chmod -R u+w "$CAPAFY_PUBLISH_WORK_DIR/staging" 2>/dev/null || true
          rm -rf "$CAPAFY_PUBLISH_WORK_DIR/staging" 2>/dev/null \
            || die "could not remove prior staging: $CAPAFY_PUBLISH_WORK_DIR/staging"
        fi
        PREPARE_RC=0
        PREPARE_OUT="$(python3 packager.py publish-submit --agent-id "$ID" --action prepare 2>&1)" || PREPARE_RC=$?
        [ "$PREPARE_RC" -eq 0 ] || die "publish-submit prepare failed (rc=$PREPARE_RC)"
        PREPARE_VALID="$(printf '%s' "$PREPARE_OUT" | python3 -c "import json,sys
try:
 p=json.loads(sys.stdin.read(),strict=False)
 print('true' if isinstance(p,dict) else 'false')
except Exception:
 print('false')")"
        PREPARE_AGENT_ID="$(printf '%s' "$PREPARE_OUT" | python3 -c "import json,sys
try: print(str(json.loads(sys.stdin.read(),strict=False).get('agent_id','') or '').strip())
except Exception: print('')")"
        PREPARE_STATUS="$(printf '%s' "$PREPARE_OUT" | python3 -c "import json,sys
try: print(str(json.loads(sys.stdin.read(),strict=False).get('status','') or '').strip())
except Exception: print('')")"
        PREPARE_SECURITY_READY="$(printf '%s' "$PREPARE_OUT" | python3 -c "import json,sys
try: print('true' if json.loads(sys.stdin.read(),strict=False).get('security_ready') is True else 'false')
except Exception: print('false')")"
        PREPARE_NEXT_ACTION="$(printf '%s' "$PREPARE_OUT" | python3 -c "import json,sys
try: print(str(json.loads(sys.stdin.read(),strict=False).get('next_action','') or '').strip())
except Exception: print('')")"
        [ "$PREPARE_VALID" = "true" ] && [ "$PREPARE_AGENT_ID" = "$ID" ] \
          && [ "$PREPARE_STATUS" = "security_review_required" ] \
          && [ "$PREPARE_SECURITY_READY" = "true" ] \
          && [ "$PREPARE_NEXT_ACTION" = "continue_upload" ] \
          || die "publish-submit prepare response failed strict security envelope; no upload attempted"
        echo "security preparation complete"

        step "[4] publish-submit continue_upload (exactly once)"
        CONTINUE_RC=0
        CONTINUE_OUT="$(python3 packager.py publish-submit --agent-id "$ID" --action continue_upload 2>&1)" || CONTINUE_RC=$?
        CONTINUE_VALID="$(printf '%s' "$CONTINUE_OUT" | python3 -c "import json,sys
try:
 p=json.loads(sys.stdin.read(),strict=False)
 print('true' if isinstance(p,dict) else 'false')
except Exception:
 print('false')")"
        CONTINUE_OK="$(printf '%s' "$CONTINUE_OUT" | python3 -c "import json,sys
try: print('true' if json.loads(sys.stdin.read(),strict=False).get('ok') is True else 'false')
except Exception: print('false')")"
        CONTINUE_AGENT_ID="$(printf '%s' "$CONTINUE_OUT" | python3 -c "import json,sys
try: print(str(json.loads(sys.stdin.read(),strict=False).get('agent_id','') or '').strip())
except Exception: print('')")"
        PUBLISH_REVIEW_URL="$(printf '%s' "$CONTINUE_OUT" | python3 -c "import json,sys
try: print(str(json.loads(sys.stdin.read(),strict=False).get('review_url','') or '').strip())
except Exception: print('')")"
        CONTINUE_BLOCKING="$(printf '%s' "$CONTINUE_OUT" | python3 -c "import json,sys
try: print(str(json.loads(sys.stdin.read(),strict=False).get('blocking_category','') or '').strip())
except Exception: print('')")"
        if [ "$CONTINUE_RC" -ne 0 ]; then
          if [ "$CONTINUE_BLOCKING" = "persist_package_uploaded_state_failed" ] && [ -n "$PUBLISH_REVIEW_URL" ]; then
            die "continue_upload returned a review_url but local package state persistence failed; upload may have occurred — do not retry continue_upload; open the review URL and check official status"
          fi
          die "continue_upload failed (rc=$CONTINUE_RC, blocking_category=${CONTINUE_BLOCKING:-unknown}); no automatic retry"
        fi
        [ "$CONTINUE_VALID" = "true" ] && [ "$CONTINUE_OK" = "true" ] \
          || die "continue_upload returned an invalid/non-success JSON result; no automatic retry"
        [ "$CONTINUE_AGENT_ID" = "$ID" ] || die "continue_upload returned agent_id=$CONTINUE_AGENT_ID, expected $ID; no automatic retry"
        [ -n "$PUBLISH_REVIEW_URL" ] || die "continue_upload succeeded without review_url; no automatic retry"
        echo "package uploaded and final review URL received for agent_id=$ID"
        ;;
      1)
      PUBLISH_REVIEW_URL="$(refresh publish)"
      [ -n "$PUBLISH_REVIEW_URL" ] || die "remote package is already uploaded but publish-refresh-url returned no review URL; upload is not retried"
      echo "remote package already uploaded ✓ — skip prepare+continue_upload; use existing final review URL"
        ;;
      *)
        die "package_uploaded readback is missing or unknown; no upload effect is allowed"
        ;;
      esac
  fi

  if [ "$CONFIG_STATE" = "0" ]; then
    step "[5] CP2 key host (drive_checkpoint2.py, final review page)"
    export CAPAFY_HOST_OPENROUTER_KEY="${CAPAFY_HOST_OPENROUTER_KEY:-$(grep '^CAPAFY_HOST_OPENROUTER_KEY=' "$LIFE_MANAGER_STATE_HOME/.env" 2>/dev/null | cut -d= -f2-)}"
    CP2="$PUBLISH_REVIEW_URL"
    timeout 150 "$VENV" "$AUTO/scripts/drive_checkpoint2.py" "$CP2" 2>&1 | grep -vE "Deprecation|warnings.warn" | tail -4
    # AUTHORITATIVE gate = server is_confirmed_config_keys, POLLED. drive_checkpoint2 can
    # exit just before the server registers the hosted key -> a one-shot read false-dies.
    poll is_confirmed_config_keys 1 12 5 || die "CP2 key host NOT confirmed (is_confirmed_config_keys!=1) — drive CP2 agentically (PUBLISHING_RUNBOOK.md)"
    echo "is_confirmed_config_keys=1 ✓"
  fi
fi

# CP3 is skipped if the agent is ALREADY submitted (platform_status=1) — makes a
# re-run idempotent (resume a half-done agent without double-submitting).
POST_CP2_STATUS="$(rstat platform_status)"
case "$POST_CP2_STATUS" in
1)
  echo "platform_status=1 already ✓ — already submitted, skip CP3"
  ;;
0)
  step "[6] CP3 submit (審査に提出, final review page) — exactly once"
  if [ -z "$PUBLISH_REVIEW_URL" ]; then
    PUBLISH_REVIEW_URL="$(refresh publish)"
  fi
  [ -n "$PUBLISH_REVIEW_URL" ] || die "publish-refresh-url --step publish returned no final review URL"
  # Prefer a fresh final review URL after CP2. If refresh is unavailable, the
  # exact URL returned by the one and only continue_upload is safe to reuse before
  # the one CP3 attempt.
  CP3="$(refresh publish)"
  if [ -z "$CP3" ]; then
    CP3="$PUBLISH_REVIEW_URL"
    echo "publish-refresh-url unavailable; reusing the exact final review URL before CP3"
  else
    PUBLISH_REVIEW_URL="$CP3"
  fi
  echo "CP3 submit attempt 1"
  VERSION_UPDATE_INFO="Updated the Agent package and workflow for this review submission."
  CP3_OUT="$(timeout 30 "$VENV" "$AUTO/scripts/drive_checkpoint3.py" "$CP3" "$VERSION_UPDATE_INFO" 2>&1)" || {
    die "CP3 raw submit failed; do not retry an uncertain external effect"
  }
  echo "CP3 driver completed; polling official status"
  poll platform_status 1 6 5 || die "CP3 official platform_status did not reach 1; do not retry an uncertain external effect"
  ;;
*)
  die "unsupported or unreadable post-CP2 platform_status=${POST_CP2_STATUS:-empty}; do not retry an uncertain external effect"
  ;;
esac

step "[7] FINAL VERIFY (remote-status, polled)"
poll platform_status 1 6 5 >/dev/null || true
python3 packager.py publish-remote-status --agent-id "$ID" 2>/dev/null | python3 -c "
import json,sys
v=json.loads(sys.stdin.read(),strict=False).get('latest_version',{})
def number(value):
    if isinstance(value,bool): return int(value)
    try: return int(value)
    except (TypeError,ValueError): return None
st=number(v.get('platform_status')); cfg=number(v.get('is_confirmed_config_keys')); sk=number(v.get('is_confirmed_skills')); au=number(v.get('audit_status')); pkg=number(v.get('package_uploaded'))
print(f'platform_status={st} skills={sk} cfg={cfg} package_uploaded={pkg} audit={au} title={str(v.get(\"title\"))[:42]}')
sys.exit(0 if (st==1 and cfg==1 and sk==1 and pkg==1) else 1)
" || die "FINAL VERIFY failed (status/cfg not 1) for agent $ID"

step "[8] ledger"
LEDGER="$LIFE_MANAGER_STATE_HOME/state/capafy-autopublish/published.jsonl"
mkdir -p "$(dirname "$LEDGER")"
python3 - "$ID" "$SKILL_NAME" "$LISTING" "$LEDGER" <<'PY'
import json,sys
aid,sn,listing,ledger=sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4]
title=sn
try:
    for ln in open(listing):
        if ln.strip().startswith('## Title'): continue
    # best-effort title from LISTING
    lines=[l.strip() for l in open(listing)]
    if '## Title' in lines:
        i=lines.index('## Title'); title=lines[i+1] if i+1<len(lines) else sn
except Exception: pass
# dedup: resuming an already-ledgered agent must not append a duplicate row
import os
if os.path.exists(ledger) and any(('"agent_id":"%s"'%aid) in l or ('"agent_id": "%s"'%aid) in l for l in open(ledger)):
    print("ledger already has", aid, "— not duplicating")
else:
    open(ledger,"a").write(json.dumps({
     "agent_id":aid,"skill":sn,"title":title,
     "status":"submitted (status=1 under review) — agentic CP1","date":__import__("datetime").date.today().isoformat()
    },ensure_ascii=False)+"\n")
    print("ledger appended", aid)
PY
[ "$?" -eq 0 ] || die "ledger write failed for agent $ID"
echo ""
echo "✅ PUBLISHED + VERIFIED: agent_id=$ID ($SKILL_NAME) — status=1 under review."
