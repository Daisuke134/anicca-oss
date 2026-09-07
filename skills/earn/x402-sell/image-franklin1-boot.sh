#!/usr/bin/env bash
# KeepAlive entrypoint for franklin1's demand-proven x402 image resale product.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
# The shared env carries a legacy wallet. This service must spend upstream and receive revenue only
# through franklin1's own wallet/home.
export ANICCA_HOME="$HOME/.blockrun"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0x3EcCAD24794ca298D25378E9902A251322ea8749"
export X402_IMAGE_PORT="8093"
# T2 fix (2026-07-25): aniccanomac-mini-1 (the physical machine's own tailscale node) has NO
# public DNS record at all (dig @8.8.8.8/@1.1.1.1 both empty) and zero CDP Bazaar listings under
# it, despite `tailscale serve status` locally claiming the /image, /openapi.json, and
# /base-usdc-balance paths were funneled on :443 -- that funnel state is stale/unauthorized
# (`tailscale funnel --bg ...` against this node now hangs forever: "Funnel is enabled, but the
# list of allowed nodes in the tailnet policy file does not include the one you are using"). No
# outside buyer could ever have reached this product. tsbridge (~/.tsbridge/tsbridge.toml) now
# runs a dedicated "franklin1-image" tsnet node for this backend (same proven pattern as
# franklin1/franklin2/claude-p) with working public DNS -- point PUBLIC_URL there and drop the
# funnel calls (tsbridge proxies the whole backend at its own root, no --set-path needed).
export X402_IMAGE_PUBLIC_URL="https://franklin1-image.tail7a0ba4.ts.net"
export X402_IMAGE_UPSTREAM_OPENAPI="http://127.0.0.1:8411/openapi.json"
export X402_BASE_USDC_BALANCE_ENABLED="1"
PIDS="$(lsof -ti tcp:8093 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
exec /usr/bin/env node "$DIR/image-server.mjs"
