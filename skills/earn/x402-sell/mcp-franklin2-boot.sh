#!/usr/bin/env bash
# KeepAlive entrypoint for franklin2's MonetizedMCP adapter.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
export ANICCA_HOME="$HOME/.franklin2-home/.blockrun"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0xe7747Fd899D8987821Bb4CB3D6aDf22565F87ce9"
export X402_PORT="8413"
export PORT="8091"
PIDS="$(lsof -ti tcp:8091 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
TAILSCALE_BIN="${LIFE_MANAGER_TAILSCALE:-$(command -v tailscale 2>/dev/null || true)}"
[ -z "$TAILSCALE_BIN" ] || "$TAILSCALE_BIN" funnel --bg --https=10000 --set-path=/mcp http://127.0.0.1:8091/mcp >/dev/null 2>&1 || true
exec /usr/bin/env node "$DIR/mcp-server.mjs"
