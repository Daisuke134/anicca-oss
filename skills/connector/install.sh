#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
STATE_HOME="${LIFE_MANAGER_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/life-manager}"
ENV_FILE="${LM_CONNECTOR_SHARED_ENV_FILE:-$STATE_HOME/.env}"
LOOP_ID="life-manager-connector-native"

case "${1:-start}" in
  status)
    exec "$HOME/loops/current/bin/lm-loop" status "$LOOP_ID"
    ;;
  start) ;;
  *) printf 'usage: install.sh connector [start|status]\n' >&2; exit 2 ;;
esac

[ "$(uname)" = Darwin ] || { printf 'Connector local install currently requires macOS\n' >&2; exit 2; }
for command in git node npm python3 gog; do
  command -v "$command" >/dev/null 2>&1 || { printf 'Connector prerequisite unavailable: %s\n' "$command" >&2; exit 2; }
done
[ -f "$ENV_FILE" ] || { printf 'Connector configuration unavailable: %s\n' "$ENV_FILE" >&2; exit 2; }
[ "$(stat -f '%Lp' "$ENV_FILE")" = 600 ] || { printf 'Connector configuration must use mode 600\n' >&2; exit 2; }
curl -fsS --max-time 2 http://127.0.0.1:9222/json/version >/dev/null || {
  printf 'Connector daily-driver unavailable on 127.0.0.1:9222\n' >&2
  exit 2
}

LIFE_MANAGER_SOURCE_REPO="$ROOT" bash "$ROOT/bin/cut-loop-release.sh" HEAD
LIFE_MANAGER_APPLY_TARGET="$LOOP_ID" "$HOME/loops/current/bin/lm-loop" apply
"$HOME/loops/current/bin/lm-loop" start "$LOOP_ID"
"$HOME/loops/current/bin/lm-loop" status "$LOOP_ID"
