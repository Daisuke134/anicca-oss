#!/bin/bash
# runtime/agentmail/launch.sh — launchd wrapper.
# Sources the Life Manager env so AGENTMAIL_WEBHOOK_SECRET (and friends) reach the
# node process without being written into the plist itself. exec'd by launchd.
set -u
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME}/.local/state/life-manager/.env}"
export AGENTMAIL_STATE_ROOT="${AGENTMAIL_STATE_ROOT:-${HOME}/.local/state/life-manager/agentmail}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
cd "$(dirname "$0")" || exit 1
NODE="${NODE_BIN:-/opt/homebrew/bin/node}"
exec "$NODE" webhook-server.ts
