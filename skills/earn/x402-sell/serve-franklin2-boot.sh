#!/usr/bin/env bash
# serve-franklin2-boot.sh — KeepAlive launchd boot of franklin2's x402 seller on :8413.
# Same recipe as serve-claude-p-boot.sh/serve-mainnet-boot.sh (x402-sell/SKILL.md), a third
# instance replication: same serve.mjs/primitives.mjs, franklin2's OWN payTo (receiving-only,
# no key needed here) and the Tailscale Funnel https port already routed for it
# (:10000 -> 8413, see `tailscale funnel status`) so the CDP Bazaar crawler gets an explicit
# https resource distinct from the other two sellers on :8411/:8412.
#
# WHY this exists (x402-seller-persistence 2026-07-14): franklin2's x402_sell earn-loop slot only
# had run.sh's in-wake `nohup ... &` boot (skills/earn/run.sh x402 block) — a real but WEAKER
# mechanism (no supervisor restarts it if it crashes; a wallet-resolution HALT or router
# reroute-avoidance can silently stop the loop from ever re-attempting the boot). The other two
# instances (claude-p, founder) already had a dedicated KeepAlive launchd job for exactly this
# reason; franklin2 had an inflow-watcher (ai.anicca.x402-inflow-watch-franklin2) expecting a
# seller to exist, but no seller-boot job was ever created. This completes that pattern.
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
# load CDP facilitator creds (existing account, same as the other two boot scripts) — never echoed
# Force franklin2's identity even when a shared environment provides another instance's home or key.
export ANICCA_HOME="$HOME/.franklin2-home/.blockrun"
unset BLOCKRUN_WALLET_KEY
export X402_PAYTO="0xe7747Fd899D8987821Bb4CB3D6aDf22565F87ce9"
# T2 fix (2026-07-25): same root cause as serve-claude-p-boot.sh -- aniccanomac-mini-1 lost Funnel
# authorization (`tailscale funnel --bg --https=10000 8413` hangs forever; sample'd the boot
# script parked in __wait4()) AND its hostname has no public DNS record at all (dig @8.8.8.8/
# @1.1.1.1 empty; zero CDP Bazaar listings under aniccanomac-mini-1). tsbridge already runs
# franklin2 as its own authorized tsnet node with working public DNS. Point PUBLIC_URL there and
# drop the funnel call (tsbridge does not need it).
export X402_PUBLIC_URL="${X402_PUBLIC_URL:-https://franklin2.tail7a0ba4.ts.net}"
export X402_NETWORK="base"
export X402_PRICE="\$0.003"
export X402_PORT="8413"
PIDS="$(lsof -ti tcp:8413 2>/dev/null || true)"; [ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
exec /usr/bin/env node "$DIR/serve-v2.mjs"
