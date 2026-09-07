#!/usr/bin/env bash
set -euo pipefail

# One redacted CFO pass. launchd owns the cadence; this wrapper owns env loading and durable,
# non-sensitive state. It never echoes Node stderr because provider errors may contain private data.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../.." && pwd)"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"
if [[ -r "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
fi
STATE_DIR="${CFO_STATE_DIR:-${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/life-manager-cfo-hourly}}"
export CFO_STATE_DIR="$STATE_DIR"
export CEO_STATE_DIR="$STATE_DIR"
# The stable release stages this canonical gate and its small Python/budget/config closure under
# the same repo root. A paused allocation exits from registry_enforce_or_exit before any provider
# or ledger work begins.
# shellcheck disable=SC1090
source "$REPO_ROOT/lib/registry-enforce.sh"
registry_enforce_or_exit cfo-hourly

APP_DIR="$REPO_ROOT/apps/life-manager"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"

if [[ -z "$NODE_BIN" || ! -x "$NODE_BIN" || ! -f "$APP_DIR/scripts/cfo-hourly-local.js" ]]; then
  mkdir -p "$STATE_DIR"
  printf '%s\n' '{"status":"failed","reportingDate":null,"revision":null,"appended":false,"delivered":false,"recovered":false}' >"$STATE_DIR/last-result.json"
  printf '%s\n' '{"status":"failed","reportingDate":null,"revision":null,"appended":false,"delivered":false,"recovered":false}'
  exit 1
fi

mkdir -p "$STATE_DIR"
TMP_RESULT="$(mktemp "${TMPDIR:-/tmp}/cfo-hourly-result.XXXXXX")"
trap 'rm -f "$TMP_RESULT"' EXIT

set +e
"$NODE_BIN" "$APP_DIR/scripts/cfo-hourly-local.js" >"$TMP_RESULT" 2>/dev/null
RC=$?
set -e

RESULT="$(tail -n 1 "$TMP_RESULT" 2>/dev/null || true)"
# Bash 3.2 treats quotes inside an unquoted =~ expression as syntax, so the former regex
# silently became ^\{status: and rejected every valid JSON summary. Use a literal prefix
# match that is stable on the macOS system Bash used by launchd.
if [[ "$RESULT" != '{"status":'* ]]; then
  RESULT='{"status":"failed","reportingDate":null,"revision":null,"appended":false,"delivered":false,"recovered":false}'
  RC=1
fi

printf '%s\n' "$RESULT" >"$STATE_DIR/last-result.json"
printf '%s\n' "$RESULT"
exit "$RC"
