#!/usr/bin/env bash
# Read-only native Connector health contract. launchd scheduling, not this script,
# decides when to invoke the next bounded pass.
set -eu
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$HERE/../.." && pwd -P)"
[ -f "$REPO_ROOT/apps/life-manager/scripts/lib/load-env-file.sh" ] || {
  printf 'Connector native health unavailable\n' >&2
  exit 2
}

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
SHARED_CDP_BASE_URL="${CLOAK_CDP_BASE_URL:-http://127.0.0.1:9222}"
# shellcheck source=/dev/null
source "$REPO_ROOT/apps/life-manager/scripts/lib/load-env-file.sh"
LM_CONNECTOR_ENV_FILE="${LM_CONNECTOR_ENV_FILE:-$HOME/.local/state/life-manager/.env}"
lm_load_env_file "$LM_CONNECTOR_ENV_FILE" || exit 2
export CLOAK_CDP_BASE_URL="$SHARED_CDP_BASE_URL"

STATE_DIR="${LM_CONNECTOR_STATE_DIR:-$HOME/.local/state/life-manager/connector-native}"
case "$STATE_DIR" in
  /*) ;;
  *) printf 'Connector native health unavailable\n' >&2; exit 2 ;;
esac
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
[ -n "$NODE_BIN" ] && [ -x "$NODE_BIN" ] || {
  printf 'Connector native health unavailable\n' >&2
  exit 2
}
STALE_MS="${LM_CONNECTOR_HEARTBEAT_STALE_MS:-900000}"
HEALTH="$($NODE_BIN "$HERE/lib/native-state.js" health "$STATE_DIR" "$STALE_MS")" || {
  printf 'Connector native health unavailable\n' >&2
  exit 2
}
if ! "$NODE_BIN" -e '
const health = JSON.parse(process.argv[1]);
process.exit(health && health.heartbeat && health.heartbeat.status === "fresh" ? 0 : 1);
' "$HEALTH"; then
  printf 'Connector native heartbeat stale\n' >&2
  exit 1
fi

GOG_BIN="${GOG_BIN:-gog}"
if ! command -v "$GOG_BIN" >/dev/null 2>&1; then
  printf 'Connector native gog unavailable\n' >&2
  exit 1
fi

if [ -n "${CONNECTOR_NATIVE_HEALTH_PROBE_BIN:-}" ]; then
  "$CONNECTOR_NATIVE_HEALTH_PROBE_BIN"
else
  "$NODE_BIN" -e '
const http = require("node:http");
const { CONNECTOR_CDP_ENDPOINT: endpoint } = require(process.argv[1]);
const request = http.get(`${endpoint}/json/version`, { timeout: 5_000 }, (response) => {
  response.resume();
  process.exitCode = response.statusCode === 200 ? 0 : 1;
});
request.on("timeout", () => request.destroy(new Error("timeout")));
request.on("error", () => { process.exitCode = 1; });
' "$REPO_ROOT/apps/life-manager/lib/connector-browser-target-controller.js"
fi
printf '{"status":"healthy"}\n'
