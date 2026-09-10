#!/usr/bin/env bash
set -euo pipefail
umask 077

core=/opt/life-manager/skills/alpaca-investment/run.py
credentials=/run/investment/credentials.json
mode="${INVESTMENT_MODE:-shadow}"
interval="${INVESTMENT_INTERVAL_SECONDS:-300}"

if [[ "${INVESTMENT_VERIFY_ONLY:-false}" == "true" ]]; then
  cd /opt/life-manager/skills/alpaca-investment
  python3 -m unittest test_prelive_replay.py test_risk_policy.py test_reporter.py
  exit 0
fi

[[ "$mode" == shadow || "$mode" == live ]] || { echo "INVESTMENT_MODE must be shadow or live" >&2; exit 64; }
[[ "$interval" == 300 ]] \
  || { echo "INVESTMENT_INTERVAL_SECONDS must be exactly 300" >&2; exit 64; }
if [[ "$mode" == live && "${I_UNDERSTAND_REAL_MONEY:-false}" != "true" ]]; then
  echo "live mode requires I_UNDERSTAND_REAL_MONEY=true" >&2
  exit 64
fi
for name in ALPACA_LIVE_API_KEY ALPACA_LIVE_API_SECRET GEMINI_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  [[ -n "${!name:-}" ]] || { echo "$name is required" >&2; exit 64; }
done

chmod 0700 /run/investment /data/investment
export INVESTMENT_CREDENTIALS_PATH="$credentials"
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["INVESTMENT_CREDENTIALS_PATH"])
path.write_text(json.dumps({"credentials": [{
    "service": "app.alpaca.markets",
    "live_endpoint": "https://api.alpaca.markets/v2",
    "live_api_key": os.environ["ALPACA_LIVE_API_KEY"],
    "live_api_secret": os.environ["ALPACA_LIVE_API_SECRET"],
}]}) + "\n", encoding="utf-8")
path.chmod(0o600)
PY

export LIFE_MANAGER_INVESTMENT_MODE="$mode"
export LM_TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN"
export TELEGRAM_ALERT_CHAT_ID="$TELEGRAM_CHAT_ID"
if [[ "$mode" == live ]]; then
  export ALPACA_INVESTMENT_LIVE_STATE_DIR=/data/investment
  export ALPACA_INVESTMENT_LIVE_CREDENTIALS_FILE="$credentials"
else
  export ALPACA_INVESTMENT_SHADOW_STATE_DIR=/data/investment
  export ALPACA_INVESTMENT_SHADOW_CREDENTIALS_FILE="$credentials"
fi

run_once() {
  python3 "$core"
}

if [[ "${INVESTMENT_ONCE:-false}" == "true" ]]; then
  run_once
  exit $?
fi

while true; do
  started_at="$(date +%s)"
  export INVESTMENT_NEXT_EPOCH="$((started_at + 300))"
  export LIFE_MANAGER_INVESTMENT_NEXT_WAKE_AT="$(python3 - <<'PY'
import os
from datetime import datetime, timezone
print(datetime.fromtimestamp(int(os.environ["INVESTMENT_NEXT_EPOCH"]), timezone.utc).isoformat())
PY
)"
  run_once || true
  delay="$(python3 - <<'PY'
import os
import time
print(max(1, int(os.environ["INVESTMENT_NEXT_EPOCH"]) - int(time.time())))
PY
)"
  sleep "$delay"
done
