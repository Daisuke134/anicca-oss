#!/usr/bin/env bash
# KeepAlive entrypoint for franklin1's MonetizedMCP adapter.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
export ANICCA_HOME="$HOME/.blockrun"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0x3EcCAD24794ca298D25378E9902A251322ea8749"
export X402_PORT="8414"
export PORT="8090"
PIDS="$(lsof -ti tcp:8090 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
# Preserve franklin1's existing `/` shop on :443 and add only the `/mcp` mount. Including the
# backend path is required because Funnel strips the public mount prefix before proxying.
/opt/homebrew/bin/tailscale funnel --bg --https=443 --set-path=/mcp http://127.0.0.1:8090/mcp >/dev/null 2>&1 || true
exec /usr/bin/env node "$DIR/mcp-server.mjs"
