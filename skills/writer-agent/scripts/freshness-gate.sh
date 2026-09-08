#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ARTICLE_ROOT="${ARTICLE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh" || exit $?
TITLE="$1"
HIST="${WRITER_ACCOUNT_HISTORY:-$WRITER_STATE_DIR/content-library/account-history.jsonl}"
[ ! -f "$HIST" ] && { echo "OK no history"; exit 0; }
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
printf '%s\n' "$TITLE" | tr ' ' '\n' | grep -v '^$' | sort -u > "$TMP"
RECENT=$(tail -200 "$HIST" 2>/dev/null | jq -r '.title // ""' | tr '\n' ' ')
OV=$(printf '%s' "$RECENT" | grep -oF -f "$TMP" -- 2>/dev/null | wc -l | tr -d ' ')
TOT=$(wc -l < "$TMP" | tr -d ' ')
R=$(( OV * 100 / (TOT + 1) ))
[ "$R" -gt 30 ] && { echo "FAIL overlap=${R}%"; exit 1; }
echo "OK overlap=${R}%"
