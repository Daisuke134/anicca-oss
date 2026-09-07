#!/usr/bin/env bash
# key_health_gate.sh — FAIL-CLOSED pre-publish gate for the Capafy host LLM key.
#
# WHY THIS EXISTS (2026-07-18, A1 root-cause):
#   Capafy rejected 4 run_online agents with "FailoverError ... billing error
#   (OpenRouter key 残高不足)" on the review smoke-test. Root cause was NOT a stale
#   key and NOT the provider-name label — it was a thin OpenRouter balance.
#   Publishing into an under-funded account
#   guarantees a re-reject. This gate stops the loop BEFORE it wastes a publish.
#
# It does three REAL checks against OpenRouter (no dry-run):
#   1. GET /key -> per-key limit_remaining must be null (unlimited) or > 0
#   2. GET /credits -> remaining = total_credits - total_usage (must be >= threshold)
#   3. POST /chat/completions anthropic/claude-sonnet-4.6 max_tokens=5 -> must return 200 + content
# NEVER prints the key. Exits 0 = healthy (publish may proceed), 1 = block (fail-closed).
#
# Usage: key_health_gate.sh [min_remaining_usd]   (default 5.00)
#
# FUNDING ALERT (#21, 2026-07-19): the gate is fail-closed but was SILENT — when the balance
# ran low the loop just stopped publishing and user never knew a top-up was needed. This gate now
# telegram-alerts user (a) as an EARLY WARNING while it still passes but is within a cushion of the
# block threshold, and (b) when it actually blocks on balance. It NEVER auto-charges — funding the
# OpenRouter card is user's decision (money out = irreversible = user gate). Alerts are deduped to
# at-most-once-per-calendar-day so a daily loop can't spam. Never prints the key.
set -uo pipefail

MIN="${1:-5.00}"
# Capafy currently asks OpenRouter to admit up to 128k completion tokens. At
# Sonnet 4.6's $15/M completion price that is $1.92 before prompt cost. Require
# enough per-key daily headroom for one worst-case admission, not merely > $0.
REQUEST_HEADROOM="${CAPAFY_REQUEST_HEADROOM_USD:-2.25}"
# Warn while still passing but getting low, so user tops up BEFORE an outage.
ALERT_CUSHION="${CAPAFY_FUNDING_ALERT_USD:-5.00}"
LIFE_MANAGER_STATE_HOME="${LIFE_MANAGER_STATE_HOME:-$HOME/.local/state/life-manager}"
STATE_DIR="$LIFE_MANAGER_STATE_HOME/state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# alert_user <remaining> <reason> — one telegram/day max (dedup marker keyed by date).
alert_user() {
  local remain="$1" reason="$2"
  local marker="$STATE_DIR/.capafy-funding-alert-$(date +%Y-%m-%d)"
  [ -f "$marker" ] && return 0   # already alerted today
  local msg="⚠️ Capafy host LLM key funding ${reason}: OpenRouter remaining \$${remain} (block threshold \$${MIN}, warn <\$${ALERT_CUSHION}). Publishing will stall until topped up. Top up (user only, no auto-charge): https://openrouter.ai/settings/credits"
  local sender="$(cd -- "$(dirname -- "$0")/../.." && pwd)/_shared/send-telegram.sh"
  if [ -x "$sender" ] && [ -n "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
    "$sender" "$msg" "$TELEGRAM_ALERT_CHAT_ID" >/dev/null 2>&1 \
      && touch "$marker"
  fi
}
KEY="${CAPAFY_HOST_OPENROUTER_KEY:-}"
if [ -z "$KEY" ]; then
  KEY="$(grep '^CAPAFY_HOST_OPENROUTER_KEY=' "$LIFE_MANAGER_STATE_HOME/.env" 2>/dev/null | cut -d= -f2-)"
fi
if [ -z "$KEY" ]; then
  echo "KEY_HEALTH=FAIL reason=CAPAFY_HOST_OPENROUTER_KEY missing"; exit 1
fi

KEY_LIMIT_REMAINING="$(curl -s --max-time 20 https://openrouter.ai/api/v1/key \
  -H "Authorization: Bearer $KEY" | python3 -c '
import json, math, sys
try:
    data = json.load(sys.stdin).get("data")
    if not isinstance(data, dict) or "limit_remaining" not in data:
        print("ERR")
    else:
        remaining = data["limit_remaining"]
        if remaining is None:
            print("UNLIMITED")
        elif isinstance(remaining, bool) or not isinstance(remaining, (int, float)) \
                or not math.isfinite(remaining):
            print("ERR")
        elif remaining <= 0:
            print("0")
        else:
            print(remaining)
except Exception:
    print("ERR")
' 2>/dev/null)"

case "$KEY_LIMIT_REMAINING" in
  0)
    alert_user "unknown" "BLOCKED (per-key limit exhausted)"
    echo "KEY_HEALTH=FAIL reason=key_limit_exhausted"; exit 1 ;;
  UNLIMITED) ;;
  ''|ERR) echo "KEY_HEALTH=FAIL reason=key_read_failed"; exit 1 ;;
  *)
    HEADROOM_OK="$(python3 -c "print('1' if float('$KEY_LIMIT_REMAINING')>=float('$REQUEST_HEADROOM') else '0')" 2>/dev/null)"
    if [ "$HEADROOM_OK" != "1" ]; then
      alert_user "$KEY_LIMIT_REMAINING" "BLOCKED (per-key limit below one request)"
      echo "KEY_HEALTH=FAIL reason=key_limit_below_request_headroom remaining=\$$KEY_LIMIT_REMAINING required=\$$REQUEST_HEADROOM"
      exit 1
    fi ;;
esac

REMAIN="$(curl -s --max-time 20 https://openrouter.ai/api/v1/credits \
  -H "Authorization: Bearer $KEY" | python3 -c "
import sys,json
try:
    c=json.load(sys.stdin).get('data',{})
    print(round(float(c.get('total_credits',0))-float(c.get('total_usage',0)),4))
except Exception:
    print('ERR')
" 2>/dev/null)"

if [ "$REMAIN" = "ERR" ] || [ -z "$REMAIN" ]; then
  echo "KEY_HEALTH=FAIL reason=credits_read_failed"; exit 1
fi

# numeric compare via python (bash can't do floats)
OK_BAL="$(python3 -c "print('1' if float('$REMAIN')>=float('$MIN') else '0')" 2>/dev/null)"
if [ "$OK_BAL" != "1" ]; then
  alert_user "$REMAIN" "BLOCKED (balance too low)"
  echo "KEY_HEALTH=FAIL reason=balance_too_low remaining=\$$REMAIN min=\$$MIN  -> TOP UP OpenRouter before publishing"
  exit 1
fi

# Passed the block threshold but within the warning cushion -> early-warn user to top up before outage.
LOW_WARN="$(python3 -c "print('1' if float('$REMAIN')<float('$ALERT_CUSHION') else '0')" 2>/dev/null)"
if [ "$LOW_WARN" = "1" ]; then
  alert_user "$REMAIN" "LOW (early warning)"
fi

PROBE="$(curl -s --max-time 30 https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"anthropic/claude-sonnet-4.6","messages":[{"role":"user","content":"say ok"}],"max_tokens":5}' \
  | python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    if 'choices' in d and d['choices'][0]['message'].get('content'):
        print('OK')
    else:
        print('ERR:'+str(d.get('error',d))[:80])
except Exception as e:
    print('ERR:'+str(e)[:80])
" 2>/dev/null)"

if [ "$PROBE" != "OK" ]; then
  echo "KEY_HEALTH=FAIL reason=live_probe_failed detail=$PROBE remaining=\$$REMAIN"; exit 1
fi

echo "KEY_HEALTH=OK remaining=\$$REMAIN (>= \$$MIN) live_probe=200"
exit 0
