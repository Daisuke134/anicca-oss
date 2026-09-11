#!/bin/bash
# Poll Stripe for new succeeded charges every 15 min
# Compare with last seen ts → if new → Telegram receipt + CFO rebuild
set -eu
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STATE_ROOT="${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/stripe-revenue-poller}"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"
DATA="$STATE_ROOT/data"
mkdir -p "$DATA"
STATE="$DATA/last-seen.txt"
[ -r "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a
[ -n "${STRIPE_SECRET_KEY:-}" ] || { echo "stripe poller: setup_required STRIPE_SECRET_KEY" >&2; exit 0; }

LAST=$(cat "$STATE" 2>/dev/null || echo "0")
NOW=$(date +%s)

# Fetch charges in last 24h
RESP=$(curl -sS "https://api.stripe.com/v1/charges?limit=20&created%5Bgt%5D=$LAST" \
  -u "$STRIPE_SECRET_KEY:" 2>&1)

NEW_COUNT=$(echo "$RESP" | jq '[.data[] | select(.status=="succeeded")] | length')
echo "[$(date +%H:%M:%S)] new charges since $LAST: $NEW_COUNT"

if [ "$NEW_COUNT" -gt 0 ]; then
  echo "$RESP" | jq -r '.data[] | select(.status=="succeeded") | "\(.id)|\(.created)|\(.amount)|\(.currency)|\(.description // .metadata.purpose // "-")"' | while IFS='|' read -r CHID CRT AMT CURR DESC; do
    echo "  💰 $CHID | $CRT | $AMT $CURR | $DESC"
    "$REPO_ROOT/skills/_shared/send-telegram.sh" \
      "💰 Stripe charge: $AMT $CURR · $DESC · verified revenue" >/dev/null 2>&1 || true

    # Auto-fulfill Sutra Candle ($3 / ¥450) — match by purpose metadata or amount+currency
    SHOULD_FULFILL=0
    case "$DESC" in
      *sutra-candle*) SHOULD_FULFILL=1 ;;
    esac
    if [ "$AMT" = "300" ] && [ "$CURR" = "usd" ]; then SHOULD_FULFILL=1; fi
    if [ "$AMT" = "450" ] && [ "$CURR" = "jpy" ]; then SHOULD_FULFILL=1; fi
    if [ "$SHOULD_FULFILL" = "1" ]; then
      bash "$REPO_ROOT/skills/sutra-candle-fulfillment/scripts/fulfill.sh" "$CHID" >> "$DATA/fulfill.log" 2>&1 &
    fi
  done
  bash "$REPO_ROOT/skills/cfo/run.sh" >/dev/null 2>&1 || true
fi

echo "$NOW" > "$STATE"
