#!/bin/bash
# runtime/agentmail/nudge-tick.sh — daily Reply-Zero sweep. Fires via launchd at 09:00 JST.
set -uo pipefail
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME}/.local/state/life-manager/.env}"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
export AGENTMAIL_STATE_ROOT="${AGENTMAIL_STATE_ROOT:-${HOME}/.local/state/life-manager/agentmail}"
NODE="${NODE_BIN:-/opt/homebrew/bin/node}"
cd "$(dirname "$0")" || exit 1
LOG="${AGENTMAIL_STATE_ROOT}/logs/agentmail-nudge.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "=== $(date -u +%FT%TZ) nudge sweep ==="
  "$NODE" nudge.ts
} >> "$LOG" 2>&1
