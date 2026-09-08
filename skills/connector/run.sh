#!/usr/bin/env bash
# Canonical bounded Connector pass. This script owns only local lifecycle state;
# the worker owns no global schedule or completion claim.
set -eu
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../.." && pwd -P)"
[ -f "$REPO_ROOT/apps/life-manager/lib/connector-minimal-production.js" ] || {
  printf 'Connector native repository unavailable\n' >&2
  exit 2
}

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
export CLOAK_CDP_BASE_URL="${CLOAK_CDP_BASE_URL:-http://127.0.0.1:9222}"
LIFE_MANAGER_STATE_HOME="${LIFE_MANAGER_STATE_HOME:-${XDG_STATE_HOME:-$HOME/.local/state}/life-manager}"
LM_CONNECTOR_SHARED_ENV_FILE="${LM_CONNECTOR_SHARED_ENV_FILE:-$LIFE_MANAGER_STATE_HOME/.env}"
export LM_CONNECTOR_SHARED_ENV_FILE

STATE_DIR="${LM_CONNECTOR_STATE_DIR:-$LIFE_MANAGER_STATE_HOME/connector-native}"
case "$STATE_DIR" in
  /*) ;;
  *) printf 'Connector native state directory unavailable\n' >&2; exit 2 ;;
esac
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
[ -n "$NODE_BIN" ] && [ -x "$NODE_BIN" ] || {
  printf 'Connector native node unavailable\n' >&2
  exit 2
}
"$NODE_BIN" "$HERE/lib/load-connector-env.js" "$LM_CONNECTOR_SHARED_ENV_FILE" || exit 2
LOCK_STALE_MS="${LM_CONNECTOR_LOCK_STALE_MS:-900000}"
OWNER_TOKEN="$($NODE_BIN "$HERE/lib/native-state.js" token)" || {
  printf 'Connector native owner unavailable\n' >&2
  exit 2
}

release_lock() {
  "$NODE_BIN" "$HERE/lib/native-state.js" release "$STATE_DIR" "$OWNER_TOKEN" >/dev/null 2>&1 || true
}

LOCK_RESULT="$($NODE_BIN "$HERE/lib/native-state.js" acquire "$STATE_DIR" "$OWNER_TOKEN" "$$" "$LOCK_STALE_MS")" || {
  printf 'Connector native lock unavailable\n' >&2
  exit 2
}
case "$LOCK_RESULT" in
  '{"status":"acquired"}') ;;
  '{"status":"busy"}') exit 75 ;;
  *) printf 'Connector native lock unavailable\n' >&2; exit 2 ;;
esac
trap release_lock EXIT

"$NODE_BIN" "$HERE/lib/native-state.js" heartbeat "$STATE_DIR" "$OWNER_TOKEN" native_started >/dev/null || exit 2
if "$NODE_BIN" "$HERE/native-pass.js" \
  --repo-root "$REPO_ROOT" \
  --state-dir "$STATE_DIR" \
  --owner-token "$OWNER_TOKEN"; then
  "$NODE_BIN" "$HERE/lib/native-state.js" heartbeat "$STATE_DIR" "$OWNER_TOKEN" worker_finished >/dev/null || exit 2
  exit 0
else
  EXIT_CODE=$?
  if [ "$EXIT_CODE" -ge 128 ]; then
    OBSERVER_ID="${OWNER_TOKEN:0:24}"
    "$NODE_BIN" "$HERE/lib/observer-envelope.js" process-crash \
      "$STATE_DIR/observer-replay.jsonl" \
      "wake:$OBSERVER_ID" "run:$OBSERVER_ID" \
      "${LM_CONNECTOR_CODE_COMMIT:-unknown}" || exit 2
    "$NODE_BIN" "$HERE/minimal-crash-report.js" \
      --repo-root "$REPO_ROOT" \
      --state-dir "$STATE_DIR" \
      --owner-token "$OWNER_TOKEN" || exit 2
  fi
  "$NODE_BIN" "$HERE/lib/native-state.js" heartbeat "$STATE_DIR" "$OWNER_TOKEN" worker_failed >/dev/null 2>&1 || true
  exit "$EXIT_CODE"
fi
