#!/usr/bin/env bash
# serve-claude-p-boot.sh — KeepAlive launchd boot of the claude-p x402 seller on :8412.
# Replication test of the x402-sell recipe (SKILL.md) for a SECOND instance on this machine:
# same serve.mjs/primitives.mjs, different payTo (claude-p's own wallet, receiving-only, no key
# needed here) and a second Tailscale Funnel https port (8443 -> 8412) so the CDP Bazaar crawler
# gets an explicit https resource distinct from the founder seller on :8411/443.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
# load CDP facilitator creds (existing account, same as serve-mainnet-boot.sh) — never echoed
# NON-DISCRIMINATION (2026-07-19): claude-p runs the SAME concentrated v2 store as franklin —
# same tool, same rail, measure external equally. Force this instance's identity so resale can
# resolve a key (the .env injects a machine-legacy home/key; this store IS claude-p).
export ANICCA_HOME="$HOME/.anicca-founder"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0x810F6D61F7606dEEE2657d3083E150a222Bc29C5"
# T2 fix (2026-07-25): the physical machine's own tailscale node (aniccanomac-mini-1) lost Funnel
# authorization -- `tailscale funnel --bg --https=8443 8412` now HANGS forever printing "Funnel is
# enabled, but the list of allowed nodes in the tailnet policy file does not include the one you
# are using" (confirmed live: sample'd the boot script parked in __wait4() for 11h, node never
# started). Its hostname also has NO public DNS record at all (dig @8.8.8.8/@1.1.1.1 both empty;
# confirmed CDP Bazaar's live catalog has zero listings under aniccanomac-mini-1) -- so even a
# successful bind was invisible to any real buyer. tsbridge (ai.anicca.tsbridge,
# ~/.tsbridge/tsbridge.toml) already runs claude-p as its OWN authorized tsnet node with a public
# DNS record (dig @8.8.8.8 claude-p.tail7a0ba4.ts.net resolves) -- same fix already proven for
# franklin1. Point PUBLIC_URL there and drop the funnel call entirely (tsbridge does not need it).
export X402_PUBLIC_URL="${X402_PUBLIC_URL:-https://claude-p.tail7a0ba4.ts.net}"
export X402_NETWORK="base"
export X402_PRICE="\$0.003"
export X402_PORT="8412"
PIDS="$(lsof -ti tcp:8412 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
exec /usr/bin/env node "$DIR/serve-v2.mjs"
