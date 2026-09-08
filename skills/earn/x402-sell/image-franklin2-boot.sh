#!/usr/bin/env bash
# KeepAlive entrypoint for franklin2's demand-proven x402 image resale product.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
# The shared env carries a legacy wallet. This service must spend upstream and receive revenue only
# through franklin2's own wallet/home.
export ANICCA_HOME="$HOME/.franklin2-home/.blockrun"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0xe7747Fd899D8987821Bb4CB3D6aDf22565F87ce9"
export X402_IMAGE_PORT="8094"
# T2 fix (2026-07-25): aniccanomac-mini-1 has no public DNS record at all and its funnel calls now
# hang forever (ACL/authorization lost) -- moved to a dedicated tsbridge tsnet node, same proven
# pattern as claude-p/franklin1/franklin2/founder's sellers.
export X402_IMAGE_PUBLIC_URL="https://franklin2-image.tail7a0ba4.ts.net"
export X402_IMAGE_UPSTREAM_OPENAPI="http://127.0.0.1:8413/openapi.json"
PIDS="$(lsof -ti tcp:8094 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
exec /usr/bin/env node "$DIR/image-server.mjs"
