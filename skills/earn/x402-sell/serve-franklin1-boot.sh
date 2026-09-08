#!/usr/bin/env bash
# serve-franklin1-boot.sh — KeepAlive launchd boot of franklin1's (~/.blockrun) x402 seller on :8414.
# Same recipe as serve-claude-p-boot.sh/serve-mainnet-boot.sh/serve-franklin2-boot.sh (x402-sell/
# SKILL.md), a fourth replication: same serve.mjs/primitives.mjs, franklin1's OWN payTo
# (receiving-only, no key needed here) and its own Tailscale Funnel https port so the CDP Bazaar
# crawler gets an explicit https resource distinct from the other sellers on :8411/:8412/:8413.
#
# WHY this exists (x402-seller-persistence 2026-07-14): franklin1 had NO dedicated x402 seller at
# all (no boot script, no port of its own in its loop plist) — when its earn-loop picked the
# x402_sell slot, run.sh's in-wake nohup fallback used the bare default port 8404, which was
# ALREADY occupied by an unrelated stray process (a different wallet's payTo). run.sh's curl-ping
# guard saw that stray process as "already up" and never booted franklin1's own seller, so a
# franklin1 x402_sell wake silently reported success while never actually exposing franklin1's OWN
# payTo. A dedicated port + dedicated KeepAlive job removes the collision permanently.
#
# WHY the URL changed again (T2b-6, 2026-07-17): Tailscale Funnel only listens on 443/8443/10000
# (official), so ":10001" below was never externally reachable — it just silently no-op'd
# `tailscale funnel --https=10001`, and this process kept advertising a dead URL forever. tsbridge
# (docs/STATUS.md T2b-2/T2b-3, launchd ai.anicca.tsbridge) now gives franklin1 its own tsnet node
# with its own :443 at https://franklin1.tail7a0ba4.ts.net, reverse-proxying to this same :8414 —
# so the fix is just pointing this seller's advertised URL at that node instead of running a second,
# broken tunnel of its own.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
# load CDP facilitator creds (existing account, same as the other boot scripts) — never echoed
# A shared legacy env can inject the wrong ANICCA_HOME and BLOCKRUN_WALLET_KEY, which
# made the resale route pay the upstream (Exa) from 0xB9dd instead of franklin1 (measured 2026-07-18:
# franklin1 got the sale, 0xB9dd paid Exa — wrong wallet). This store IS franklin1: force its identity
# so resolve-identity() derives franklin1's own key (0x3EcCAD24) for upstream payments + float guard.
export ANICCA_HOME="$HOME/.blockrun"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0x3EcCAD24794ca298D25378E9902A251322ea8749"
export X402_PUBLIC_URL="${X402_PUBLIC_URL:-https://franklin1.tail7a0ba4.ts.net}"
export X402_NETWORK="base"
export X402_PRICE="\$0.003"
export X402_PORT="8414"
PIDS="$(lsof -ti tcp:8414 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
exec /usr/bin/env node "$DIR/serve-v2.mjs"
