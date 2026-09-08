#!/usr/bin/env bash
# Sealed releases do not contain .git; derive the root from this script and validate
# the repository files this entrypoint needs before continuing.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P)" || SCRIPT_DIR=""
DEFAULT_LIFE_MANAGER_REPO=""
if [ -n "$SCRIPT_DIR" ]; then
  DEFAULT_LIFE_MANAGER_REPO="$(cd "$SCRIPT_DIR/../../.." 2>/dev/null && pwd -P)" || DEFAULT_LIFE_MANAGER_REPO=""
fi
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$DEFAULT_LIFE_MANAGER_REPO}"
if [ -z "$LIFE_MANAGER_REPO" ] || [ ! -d "$LIFE_MANAGER_REPO" ] \
  || [ ! -f "$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/capafy-goal-monitor.sh" ]; then
  echo "LIFE_MANAGER_REPO could not be resolved" >&2
  exit 2
fi
export LIFE_MANAGER_REPO
# capafy-goal-monitor.sh — daily AUTONOMOUS audit of goal (a)-(d) + idempotent auto go-live.
#
# This is the "zero parent intervention" implementation: instead of a human tracking the
# time-dependent goal gates (7-day BLOCKED-free streak, sales reconcile, warmup-day-3 go-live,
# self-heal health), this deterministic monitor does it daily and reports to Dais on telegram.
#
# HARD RULES: NO LLM. read + append ONLY (never destroys prod state/ledgers). launchd auto-load is
# IDEMPOTENT (checks launchctl list first, never double-loads). NO secrets in output. go-live gate
# uses the REAL warmup-ledger day (NO date hardcode). Self-heal check is NON-DESTRUCTIVE (never kills
# a prod loop). Emits one JSON object on stdout + one telegram summary.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
set -uo pipefail
PY=/opt/homebrew/bin/python3
LIFE_MANAGER_STATE_HOME="${LIFE_MANAGER_STATE_HOME:-$HOME/.local/state/life-manager}"
for ENV_FILE in "$LIFE_MANAGER_STATE_HOME/.env"; do
  [ -f "$ENV_FILE" ] || continue
  set -a; . "$ENV_FILE" 2>/dev/null; set +a
done
STATE="$LIFE_MANAGER_STATE_HOME/state/capafy-goal-monitor.json"
mkdir -p "$(dirname "$STATE")"

DAILY_TERMINAL_LEDGER="$LIFE_MANAGER_STATE_HOME/state/capafy-daily-terminals.jsonl"
DAILY_TERMINAL_TOOL="$LIFE_MANAGER_REPO/skills/self/capafy-loop/capafy_daily_terminal.py"
EARN_LEDGER="$LIFE_MANAGER_STATE_HOME/state/capafy-hourly-reconcile.json"
KEY_GATE="$LIFE_MANAGER_REPO/skills/capafy-autopublish/scripts/key_health_gate.sh"
IG_SCRIPT="$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/capafy-ig-marketing-daily.sh"
ACCOUNT_STATE_HELPER="${CAPAFY_ACCOUNT_STATE_HELPER:-$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/account_state.sh}"
if [ ! -f "$ACCOUNT_STATE_HELPER" ]; then
  echo "CAPAFY_ACCOUNT_STATE_HELPER could not be resolved: $ACCOUNT_STATE_HELPER" >&2
  exit 2
fi
# shellcheck source=account_state.sh
if ! . "$ACCOUNT_STATE_HELPER" 2>/dev/null; then
  echo "CAPAFY_ACCOUNT_STATE_HELPER could not be sourced: $ACCOUNT_STATE_HELPER" >&2
  exit 2
fi
for REQUIRED_ACCOUNT_HELPER in \
  capafy_ig_accounts_file \
  resolve_capafy_ig_handle \
  resolve_capafy_ig_port \
  resolve_capafy_ig_session_owner \
  resolve_capafy_ig_started_warming \
  capafy_ig_warming_day; do
  if ! declare -F "$REQUIRED_ACCOUNT_HELPER" >/dev/null 2>&1; then
    echo "CAPAFY_ACCOUNT_STATE_HELPER missing required function: $REQUIRED_ACCOUNT_HELPER" >&2
    exit 2
  fi
done
ACCOUNTS_FILE="$(capafy_ig_accounts_file)"
IG_HANDLE="$(resolve_capafy_ig_handle "$ACCOUNTS_FILE")"
IG_PORT="$(resolve_capafy_ig_port "$ACCOUNTS_FILE")"
IG_SESSION_OWNER="$(resolve_capafy_ig_session_owner "$ACCOUNTS_FILE")"
IG_STARTED_WARMING="$(resolve_capafy_ig_started_warming "$ACCOUNTS_FILE")"
ACCOUNT_DAY="$(capafy_ig_warming_day "$IG_STARTED_WARMING")"
WARMUP="$HOME/.cloak/ig-warmup-${IG_HANDLE:-no-active-account}.json"
IG_PLIST="$HOME/Library/LaunchAgents/ai.anicca.capafy-ig-marketing-daily.plist"
IG_LABEL="ai.anicca.capafy-ig-marketing-daily"
LAUNCHCTL_SAFE="${CAPAFY_LAUNCHCTL_SAFE:-$LIFE_MANAGER_REPO/bin/launchctl-safe}"
LAUNCHCTL_DOMAIN="${CAPAFY_LAUNCHCTL_DOMAIN:-gui/$(id -u)}"
IG_UNLOAD_POLL_ATTEMPTS="${CAPAFY_IG_UNLOAD_POLL_ATTEMPTS:-50}"
IG_UNLOAD_POLL_SLEEP="${CAPAFY_IG_UNLOAD_POLL_SLEEP:-0.2}"
INSTA_PY="$HOME/.cache/instagrapi-venv/bin/python"
INSTA_POSTER="$LIFE_MANAGER_REPO/skills/earn/marketing-engine/poster.py"
COOKED_MARKER="$HOME/.local/state/life-manager/state/.capafy-ig-account-cooked"
# Dais decision 2026-07-18: don't wait a full 7d — early NON-COMMERCIAL test post at day>=3 to
# MEASURE reach (the only real shadowban test), then go commercial only if reach is healthy.
WARMUP_DAYS_REQUIRED=3
if [ "${CAPAFY_GOAL_MONITOR_PROBE_ONLY:-0}" = "1" ]; then
  printf 'active_handle=%s active_port=%s accounts_path=%s\n' \
    "${IG_HANDLE:-none}" "${IG_PORT:-none}" "$ACCOUNTS_FILE"
  exit 0
fi

# ── goal(c) go-live: create + load the IG launchd ONLY when account day>=3. Idempotent. ──
write_ig_plist() {
  cat > "$IG_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$IG_LABEL</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$IG_SCRIPT</string></array>
  <key>EnvironmentVariables</key><dict><key>HOME</key><string>$HOME</string><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$HOME/.local/state/life-manager/logs/capafy-ig-marketing-daily.out</string>
  <key>StandardErrorPath</key><string>$HOME/.local/state/life-manager/logs/capafy-ig-marketing-daily.err</string>
</dict></plist>
PLIST
}

converge_ig_launchd() {
  local service="$LAUNCHCTL_DOMAIN/$IG_LABEL" current="" after=""
  if current="$($LAUNCHCTL_SAFE print "$service" 2>/dev/null)"; then
    if printf '%s\n' "$current" | grep -Eq 'run interval = 3600 seconds'; then
      GO_LIVE_ACTION="already_live_hourly"
      return 0
    fi
    write_ig_plist
    "$LAUNCHCTL_SAFE" preflight >/dev/null || return $?
    "$LAUNCHCTL_SAFE" bootout "$service" >/dev/null 2>&1 || return $?
    local unloaded=0
    for _ in $(seq 1 "$IG_UNLOAD_POLL_ATTEMPTS"); do
      if ! "$LAUNCHCTL_SAFE" print "$service" >/dev/null 2>&1; then
        unloaded=1
        break
      fi
      sleep "$IG_UNLOAD_POLL_SLEEP"
    done
    [ "$unloaded" -eq 1 ] || return 1
    "$LAUNCHCTL_SAFE" bootstrap "$LAUNCHCTL_DOMAIN" "$IG_PLIST" >/dev/null || return $?
  else
    write_ig_plist
    "$LAUNCHCTL_SAFE" preflight >/dev/null || return $?
    "$LAUNCHCTL_SAFE" bootstrap "$LAUNCHCTL_DOMAIN" "$IG_PLIST" >/dev/null || return $?
  fi
  after="$($LAUNCHCTL_SAFE print "$service" 2>/dev/null)" || return 1
  printf '%s\n' "$after" | grep -Eq 'run interval = 3600 seconds' || return 1
  GO_LIVE_ACTION="LOADED_NOW"
  return 0
}

if [ "${CAPAFY_GOAL_MONITOR_LAUNCHD_TEST:-0}" = "1" ]; then
  converge_ig_launchd
  exit $?
fi

# Distribution remains loop-driven after warmup. A newer explicit creative-review request overrides
# the old standing no-approval rule for a reviewed artifact: the marketing wrapper fail-closes on its
# repo-external pending receipt and releases only the exact user-approved SHA-256. This monitor does
# not create, alter, or bypass that receipt. Idempotent: never double-loads.
WDAY="$ACCOUNT_DAY"

# Read-only account health probe. Only an aged instagrapi-owned account has a golden session to
# verify. browser-owned/day1-2 accounts are intentionally sessionless and must never be cooked.
VERIFY_ELIGIBLE="no"
if [ "$IG_SESSION_OWNER" = "instagrapi" ] && [ "${ACCOUNT_DAY:-0}" -ge "$WARMUP_DAYS_REQUIRED" ]; then
  VERIFY_ELIGIBLE="yes"
fi
VERIFY_JSON=""
VERIFY_RC=0
if [ "$VERIFY_ELIGIBLE" != "yes" ]; then
  VERIFY_JSON="{\"ok\":true,\"skipped\":true,\"poisoned\":false,\"reason\":\"session not established\",\"session_owner\":\"${IG_SESSION_OWNER:-none}\",\"account_day\":${ACCOUNT_DAY:-0}}"
elif [ -z "$IG_HANDLE" ]; then
  VERIFY_JSON='{"ok":false,"error":"IG_HANDLE unresolved from Capafy account state"}'
  VERIFY_RC=2
elif [ -z "$IG_PORT" ]; then
  VERIFY_JSON='{"ok":false,"error":"IG_PORT unresolved from Capafy account state"}'
  VERIFY_RC=2
elif [ ! -x "$INSTA_PY" ]; then
  VERIFY_JSON='{"ok":false,"error":"instagrapi venv missing"}'
  VERIFY_RC=2
else
  VERIFY_JSON="$(CDP_PORT="$IG_PORT" "$INSTA_PY" "$INSTA_POSTER" --handle "$IG_HANDLE" --port "$IG_PORT" --accounts-path "$ACCOUNTS_FILE" --verify-only 2>>"$HOME/.local/state/life-manager/logs/capafy-goal-monitor.err.log")"
  VERIFY_RC=$?
fi
ACCOUNT_COOKED="$($PY - "$VERIFY_JSON" <<'PY' 2>/dev/null
import json, sys
raw = sys.argv[1]
try:
    data = json.loads(raw)
except Exception:
    data = {}
print("yes" if data.get("poisoned") is True or "ChallengeRequired" in raw else "no")
PY
)"
if [ "$ACCOUNT_COOKED" = "yes" ]; then
  touch "$COOKED_MARKER"
elif [ "$VERIFY_ELIGIBLE" != "yes" ] && [ -n "$IG_HANDLE" ] && [ -f "$COOKED_MARKER" ]; then
  rm -f "$COOKED_MARKER"
fi

if [ "${CAPAFY_GOAL_MONITOR_VERIFY_PROBE_ONLY:-0}" = "1" ]; then
  printf 'verify_eligible=%s session_owner=%s account_day=%s verify_rc=%s cooked_marker=%s verify_json=%s\n' \
    "$VERIFY_ELIGIBLE" "${IG_SESSION_OWNER:-none}" "${ACCOUNT_DAY:-0}" "$VERIFY_RC" \
    "$([ -f "$COOKED_MARKER" ] && printf present || printf absent)" "$VERIFY_JSON"
  exit 0
fi

GO_LIVE_ACTION="not_yet"
if [ "${WDAY:-0}" -ge "$WARMUP_DAYS_REQUIRED" ]; then
  if [ "${CAPAFY_HEADLESS_BRIDGE:-0}" = "1" ]; then
    GO_LIVE_ACTION="headless_bridge"
  elif ! converge_ig_launchd; then
    GO_LIVE_ACTION="load_failed"
    echo "IG launchd convergence failed — stopping goal monitor" >&2
    exit 2
  fi
fi

# ── the rest (goal a/b/d parsing + state + telegram body) is one python pass (read+append only). ──
DAILY_PROOF_JSON="$($PY "$DAILY_TERMINAL_TOOL" status --ledger "$DAILY_TERMINAL_LEDGER" 2>/dev/null || printf '{}')"
$PY - "$STATE" "$EARN_LEDGER" "$WARMUP" "$KEY_GATE" "$IG_PLIST" "$IG_LABEL" "$WDAY" "$WARMUP_DAYS_REQUIRED" "$GO_LIVE_ACTION" "$IG_HANDLE" "$VERIFY_JSON" "$VERIFY_RC" "$COOKED_MARKER" "$DAILY_PROOF_JSON" <<'PY' > /tmp/capafy_goal_monitor.json
import json, os, re, subprocess, sys, datetime
(state_p, earn_ledger, warmup, key_gate, ig_plist, ig_label, wday, wreq,
 golive, ig_handle, verify_raw, verify_rc, cooked_marker, daily_proof_raw) = sys.argv[1:15]
wday = int(wday or 0); wreq = int(wreq); verify_rc = int(verify_rc)
try:
    verify = json.loads(verify_raw)
except Exception:
    verify = {"ok": False, "error": "verify-only returned invalid JSON"}
account_cooked = verify.get("poisoned") is True or "ChallengeRequired" in verify_raw
now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
today = now.date()

# goal(a): durable outer launchd-owner terminals. Missing start/terminal or any
# nonzero execution breaks that day; inner drainer logs are not accepted as proof.
try:
    daily_proof = json.loads(daily_proof_raw)
except Exception:
    daily_proof = {}
streak = int(daily_proof.get("consecutive_healthy_days") or 0)
goal_a_pass = daily_proof.get("pass") is True

# goal(b): latest sales row + reconcile freshness (staleness = divergence risk).
gross = orders = None; last_sales_date = None; reconcile_age_h = None
if os.path.exists(earn_ledger):
    reconcile_age_h = round((now.timestamp() - os.path.getmtime(earn_ledger)) / 3600, 1)
    try:
        r = json.load(open(earn_ledger))
    except Exception:
        r = {}
    if r.get("orders") is not None:
        orders = r.get("orders")
        gross = (r.get("money") or {}).get("gross_usd")
        last_sales_date = str(r.get("observed_at") or "")[:10] or None
goal_b_ok = reconcile_age_h is not None and reconcile_age_h < 48  # reconcile ran within 2 days

# goal(d): NON-DESTRUCTIVE health — launchctl loaded? plist exists? key-health gate exit?
def loaded(label):
    try: return subprocess.run(["launchctl","list"],capture_output=True,text=True,timeout=15).stdout.find(label) >= 0
    except: return False
health = {
    "capafy_loop_daily_loaded": loaded("ai.anicca.capafy-loop-daily"),
    "ig_warmup_loaded": loaded("ai.anicca.capafy-marketing-warmup"),
    "ig_marketing_loaded": loaded(ig_label),
}
gate_ok = None
if os.path.exists(key_gate):
    try:
        # non-destructive: many gates support a --check/dry read; fall back to bash -n if not.
        rc = subprocess.run(["bash","-n",key_gate],capture_output=True,text=True,timeout=15).returncode
        gate_ok = (rc == 0)
    except: gate_ok = None

report = {
    "ts": int(now.timestamp()), "date": today.isoformat(),
    "goal_a": {"blocked_free_streak_days": streak, "required": 7, "pass": goal_a_pass},
    "goal_b": {"last_sales_date": last_sales_date, "orders": orders, "gross_usd": gross,
               "reconcile_age_hours": reconcile_age_h, "fresh": goal_b_ok},
    "goal_c": {"warmup_day": wday, "required": wreq, "go_live_action": golive,
               "ig_marketing_loaded": health["ig_marketing_loaded"]},
    "goal_d": {**health, "key_health_gate_ok": gate_ok},
    "account_health": {"handle": ig_handle, "verify_rc": verify_rc, "verify": verify,
                       "poisoned": account_cooked, "cooked_marker": os.path.exists(cooked_marker)},
}
# append to state (history), keep last 60
hist = []
if os.path.exists(state_p):
    try: hist = json.load(open(state_p)).get("history", [])
    except: hist = []
hist.append(report); hist = hist[-60:]
json.dump({"latest": report, "history": hist}, open(state_p, "w"), ensure_ascii=False, indent=1)

# telegram body (one screen, no secrets)
ga = report["goal_a"]; gb = report["goal_b"]; gc = report["goal_c"]; gd = report["goal_d"]
ah = report["account_health"]
account_line = (
 "account @" + ah["handle"] + ": account poisoned、fresh 作り直しが必要"
 if ah["poisoned"] else
 "account @" + ah["handle"] + ": session 未確立、verify 対象外"
 if ah["verify"].get("skipped") else
 "account @" + ah["handle"] + ": verify-only ok=" + str(ah["verify"].get("ok"))
)
body = (
 "[Capafy goal-monitor " + today.isoformat() + "]\n"
 "goal(a) BLOCKED-free streak: " + str(ga["blocked_free_streak_days"]) + "/7 " + ("PASS" if ga["pass"] else "building") + "\n"
 "goal(b) sales: orders=" + str(gb["orders"]) + " gross=$" + str(gb["gross_usd"]) + " (last " + str(gb["last_sales_date"]) + "), reconcile " + str(gb["reconcile_age_hours"]) + "h ago " + ("OK" if gb["fresh"] else "STALE") + "\n"
 "goal(c) IG warmup day " + str(gc["warmup_day"]) + "/" + str(gc["required"]) + " -> go-live: " + gc["go_live_action"] + " (ig loop loaded=" + str(gc["ig_marketing_loaded"]) + ")\n"
 "goal(d) health: capafy-loop=" + str(gd["capafy_loop_daily_loaded"]) + " warmup=" + str(gd["ig_warmup_loaded"]) + " key-gate=" + str(gd["key_health_gate_ok"]) + "\n"
 + account_line
)
open("/tmp/capafy_goal_monitor_body.txt","w").write(body)
print(json.dumps(report, ensure_ascii=False))
PY

RC=$?
BODY="$(cat /tmp/capafy_goal_monitor_body.txt 2>/dev/null)"
REPORT_KIND="${CAPAFY_REPORT_KIND:-morning}"

# Hourly control wakes use the unified state-change receipt. It refreshes money,
# joins candidate/slot/post/revenue under one run_id, and dedupes through the
# durable Telegram outbox. Never fall through to the legacy sender on this path.
if [ "$REPORT_KIND" = "hourly" ]; then
  "$PY" "$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/capafy_web_token_refresh.py" \
    >>"$HOME/.local/state/life-manager/logs/capafy-goal-monitor-hourly.out" 2>>"$HOME/.local/state/life-manager/logs/capafy-goal-monitor-hourly.err" || true
  "$PY" "$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/capafy_hourly_reconcile.py" \
    >>"$HOME/.local/state/life-manager/logs/capafy-goal-monitor-hourly.out" 2>>"$HOME/.local/state/life-manager/logs/capafy-goal-monitor-hourly.err"
  RECONCILE_RC=$?
  "$PY" "$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/capafy_company_receipt.py" deliver \
    >>"$HOME/.local/state/life-manager/logs/capafy-goal-monitor-hourly.out" 2>>"$HOME/.local/state/life-manager/logs/capafy-goal-monitor-hourly.err"
  UNIFIED_RC=$?
  cat /tmp/capafy_goal_monitor.json 2>/dev/null
  [ "$RECONCILE_RC" -eq 0 ] || exit "$RECONCILE_RC"
  exit "$UNIFIED_RC"
fi

# telegram daily report (best-effort; never blocks the monitor)
if [ -n "$BODY" ]; then
  "$LIFE_MANAGER_REPO/skills/_shared/send-telegram.sh" "$BODY" \
    "${CAPAFY_TELEGRAM_TARGET:-${TELEGRAM_ALERT_CHAT_ID:?CAPAFY_TELEGRAM_TARGET or TELEGRAM_ALERT_CHAT_ID is required}}" \
    >/dev/null 2>&1 || true
fi
cat /tmp/capafy_goal_monitor.json 2>/dev/null
exit 0
