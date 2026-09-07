#!/usr/bin/env bash
# serve-mainnet-boot.sh — KeepAlive launchd boot of the founder x402 RESEARCH seller on :8411.
# Uses the CDP facilitator (CDP_API_KEY_ID/SECRET from the Life Manager private env) → settles on Base mainnet AND
# lists the endpoint in the x402 Bazaar discovery layer (so buyer agents FIND it). payTo = founder 0x810f
# (USDC lands in our wallet; CDP only facilitates + catalogs, never custodies — no private key on the server).
# Product = $0 research-product.mjs (Wikipedia + HN + Jina). Public via Tailscale Funnel.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
# load CDP facilitator creds (existing account) — never echoed
export X402_PAYTO="0x810f6d61f7606deee2657d3083e150a222bc29c5"
# T2 fix (2026-07-25): aniccanomac-mini-1 (this machine's own tailscale node) has NO public DNS
# record at all (dig @8.8.8.8/@1.1.1.1 both empty) -- `tailscale funnel --bg 8411` against it now
# hangs forever ("Funnel is enabled, but the list of allowed nodes in the tailnet policy file does
# not include the one you are using") and the CDP Bazaar's live catalog has zero listings under
# this hostname, so no outside buyer could ever have reached this product even though the local
# server was healthy. tsbridge (~/.tsbridge/tsbridge.toml) now runs a dedicated "founder" tsnet
# node for this backend (same proven pattern as franklin1/franklin2/claude-p/franklin1-image) with
# working public DNS. Point PUBLIC_URL there and drop the funnel call.
export X402_PUBLIC_URL="${X402_PUBLIC_URL:-https://founder.tail7a0ba4.ts.net}"
export X402_NETWORK="base"
export X402_PRICE="\$0.003"
export X402_PORT="8411"
PIDS="$(lsof -ti tcp:8411 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
exec /usr/bin/env node "$DIR/serve.mjs"
