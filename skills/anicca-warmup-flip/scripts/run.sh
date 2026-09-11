#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
STATE_ROOT="${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/warmup-flip-daily}"
STATE="$STATE_ROOT/state/postiz-integrations.json"
[ -f "$STATE" ] || { echo "❌ $STATE not found"; exit 3; }
exec python3 "$HERE/warmup_flip.py" \
  --state "$STATE" \
  --sender "${WARMUP_FLIP_TELEGRAM_SENDER:-$REPO_ROOT/skills/_shared/send-telegram.sh}"
