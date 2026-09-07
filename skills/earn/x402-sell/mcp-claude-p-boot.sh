#!/usr/bin/env bash
# KeepAlive entrypoint for claude-p's MonetizedMCP adapter.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
export ANICCA_HOME="$HOME/.anicca-founder"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0x810F6D61F7606dEEE2657d3083E150a222Bc29C5"
export X402_PORT="8412"
export PORT="8092"
PIDS="$(lsof -ti tcp:8092 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
/opt/homebrew/bin/tailscale funnel --bg --https=8443 --set-path=/mcp http://127.0.0.1:8092/mcp >/dev/null 2>&1 || true
exec /usr/bin/env node "$DIR/mcp-server.mjs"
