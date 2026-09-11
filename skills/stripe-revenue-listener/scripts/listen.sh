#!/bin/bash
# Stripe webhook listener — captures charge.succeeded + customer.subscription.created
# Forwards to local handler that triggers CFO rebuild + Telegram receipt
set -eu
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
STATE_ROOT="${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/stripe-revenue-listener}"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"
DATA="$STATE_ROOT/data"
mkdir -p "$DATA"
[ -r "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a
EVENTS="$DATA/events.jsonl"
EVENTS_LOCK="$DATA/.events-ledger.lock"
MAX_EVENTS_BYTES="${STRIPE_EVENTS_MAX_BYTES:-134217728}"
case "$MAX_EVENTS_BYTES" in
  ''|*[!0-9]*|0) MAX_EVENTS_BYTES=134217728 ;;
esac

next_archive() {
  local stamp="$1" candidate suffix=0
  candidate="$DATA/events-$stamp.jsonl.gz"
  while [ -e "$candidate" ]; do
    suffix=$((suffix + 1))
    candidate="$DATA/events-$stamp.$suffix.jsonl.gz"
  done
  printf '%s\n' "$candidate"
}

acquire_events_lock() {
  local attempt=0 old_pid lock_mtime now
  while [ "$attempt" -lt 120 ]; do
    if mkdir "$EVENTS_LOCK" 2>/dev/null; then
      printf '%s\n' "$$" > "$EVENTS_LOCK/pid"
      return 0
    fi
    old_pid=$(cat "$EVENTS_LOCK/pid" 2>/dev/null || true)
    if [ -n "$old_pid" ]; then
      if kill -0 "$old_pid" 2>/dev/null; then
        sleep 0.05
        attempt=$((attempt + 1))
        continue
      fi
      rm -f "$EVENTS_LOCK/pid" 2>/dev/null || true
      rmdir "$EVENTS_LOCK" 2>/dev/null || true
      continue
    fi
    lock_mtime=$(stat -f%m "$EVENTS_LOCK" 2>/dev/null || echo 0)
    now=$(date +%s)
    if [ "$lock_mtime" -gt 0 ] && [ "$now" -ge "$lock_mtime" ] && [ "$((now - lock_mtime))" -ge 300 ]; then
      rm -f "$EVENTS_LOCK/pid" 2>/dev/null || true
      rmdir "$EVENTS_LOCK" 2>/dev/null || true
      continue
    fi
    sleep 0.05
    attempt=$((attempt + 1))
  done
  return 1
}

release_events_lock() {
  local owner
  owner=$(cat "$EVENTS_LOCK/pid" 2>/dev/null || true)
  if [ "$owner" = "$$" ]; then
    rm -f "$EVENTS_LOCK/pid" 2>/dev/null || true
    rmdir "$EVENTS_LOCK" 2>/dev/null || true
  fi
}

recover_rotating() {
  local orphan archive tmp stamp
  for orphan in "$DATA"/events-*.jsonl.rotating; do
    [ -f "$orphan" ] || continue
    if [ ! -s "$EVENTS" ]; then
      mv "$orphan" "$EVENTS" 2>/dev/null || true
      continue
    fi
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    archive="$(next_archive "$stamp")"
    tmp="$archive.tmp.$$"
    if gzip -c "$orphan" > "$tmp" 2>/dev/null && mv "$tmp" "$archive" 2>/dev/null; then
      rm -f "$orphan" 2>/dev/null || true
    else
      rm -f "$tmp" 2>/dev/null || true
    fi
  done
}

rotate_events_if_needed() {
  local size stamp archive tmp rotating
  size=$(stat -f%z "$EVENTS" 2>/dev/null || echo 0)
  case "$size" in ''|*[!0-9]*) return 0 ;; esac
  [ "$size" -gt "$MAX_EVENTS_BYTES" ] || return 0
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="$(next_archive "$stamp")"
  rotating="$DATA/events-$stamp.jsonl.rotating"
  mv "$EVENTS" "$rotating" 2>/dev/null || return 0
  : > "$EVENTS"
  tmp="$archive.tmp.$$"
  if gzip -c "$rotating" > "$tmp" 2>/dev/null && mv "$tmp" "$archive" 2>/dev/null; then
    rm -f "$rotating" 2>/dev/null || true
  else
    rm -f "$tmp" 2>/dev/null || true
  fi
}

if acquire_events_lock; then
  recover_rotating
  release_events_lock
else
  echo "stripe listener: event ledger lock unavailable" >&2
  exit 1
fi

append_event() {
  local line="$1"
  while ! acquire_events_lock; do
    sleep 1
  done
  rotate_events_if_needed
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" >> "$EVENTS"
  release_events_lock
}

# Forward to local script via stripe CLI listen
STRIPE_BIN="${STRIPE_BIN:-$(command -v stripe || true)}"
[ -n "$STRIPE_BIN" ] && [ -x "$STRIPE_BIN" ] || { echo "stripe listener: setup_required STRIPE_BIN" >&2; exit 0; }
[ -n "${STRIPE_SECRET_KEY:-}" ] || { echo "stripe listener: setup_required STRIPE_SECRET_KEY" >&2; exit 0; }
"$STRIPE_BIN" listen \
  --api-key "$STRIPE_SECRET_KEY" \
  --forward-to "https://hooks.localhost/anicca-stripe" \
  --events "charge.succeeded,customer.subscription.created,invoice.paid" \
  --print-json \
  2>&1 | while read -r line; do
    append_event "$line"

    # Detect successful charge
    if echo "$line" | grep -q '"type":"charge.succeeded"'; then
      AMT=$(echo "$line" | jq -r '.data.object.amount // 0')
      CURR=$(echo "$line" | jq -r '.data.object.currency // "?"')
      DESC=$(echo "$line" | jq -r '.data.object.description // .data.object.metadata.purpose // "?"')
      echo "🎯 CHARGE SUCCEEDED: $AMT $CURR ($DESC)"

      "$REPO_ROOT/skills/_shared/send-telegram.sh" \
        "💰 Stripe charge: $AMT $CURR · $DESC · confirmed revenue" >/dev/null 2>&1 || true

      # CFO rebuild
      bash "$REPO_ROOT/skills/cfo/run.sh" >/dev/null 2>&1 || true
    fi
  done
