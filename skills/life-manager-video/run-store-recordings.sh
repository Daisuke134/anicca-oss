#!/bin/bash
set -a; . "${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}" 2>/dev/null; set +a
if [ -z "${TELNYX_API_KEY:-}" ]; then
  printf '%s\n' '{"status":"skipped","reason":"telnyx_not_configured"}'
  exit 0
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$HERE/store-recordings.py"
