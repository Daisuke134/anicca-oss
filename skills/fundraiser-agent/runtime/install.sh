#!/usr/bin/env bash
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
LOOP_ID="fundraiser"

case "${1:-start}" in
  status)
    exec "$HOME/loops/current/bin/lm-loop" status "$LOOP_ID"
    ;;
  start) ;;
  *) printf 'usage: install.sh fundraiser [start|status]\n' >&2; exit 2 ;;
esac

[ "$(uname)" = Darwin ] || { printf 'Fundraiser local install currently requires macOS\n' >&2; exit 2; }
for command in git node npm python3 gog; do
  command -v "$command" >/dev/null 2>&1 || { printf 'Fundraiser prerequisite unavailable: %s\n' "$command" >&2; exit 2; }
done
curl -fsS --max-time 2 http://127.0.0.1:9222/json/version >/dev/null || {
  printf 'Fundraiser daily-driver unavailable on 127.0.0.1:9222\n' >&2
  exit 2
}

LIFE_MANAGER_SOURCE_REPO="$ROOT" bash "$ROOT/bin/cut-loop-release.sh" HEAD
LIFE_MANAGER_APPLY_TARGET="$LOOP_ID" "$HOME/loops/current/bin/lm-loop" apply
"$HOME/loops/current/bin/lm-loop" start "$LOOP_ID"
"$HOME/loops/current/bin/lm-loop" status "$LOOP_ID"
