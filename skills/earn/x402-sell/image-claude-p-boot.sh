#!/usr/bin/env bash
# KeepAlive entrypoint for claude-p's demand-proven x402 image resale product.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
# The shared env carries a legacy wallet. This service must spend upstream and receive revenue only
# through claude-p's own wallet/home.
export ANICCA_HOME="$HOME/.anicca-founder"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0x810F6D61F7606dEEE2657d3083E150a222Bc29C5"
export X402_IMAGE_PORT="8095"
# T2 fix (2026-07-25): aniccanomac-mini-1 has no public DNS record at all and its funnel calls now
# hang forever (ACL/authorization lost) -- moved to a dedicated tsbridge tsnet node, same proven
# pattern as claude-p/franklin1/franklin2/founder's sellers.
export X402_IMAGE_PUBLIC_URL="https://claude-p-image.tail7a0ba4.ts.net"
export X402_IMAGE_UPSTREAM_OPENAPI="http://127.0.0.1:8412/openapi.json"
PIDS="$(lsof -ti tcp:8095 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
exec /usr/bin/env node "$DIR/image-server.mjs"
