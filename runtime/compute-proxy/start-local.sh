#!/usr/bin/env bash
# anicca local-default compute proxy — fully local, free, no server/API key.
#
# Anicca pays its OWN compute via BlockRun x402 from its OWN
# wallet — exactly like Franklin. The user provides only SHELTER (this device);
# Anicca buys its own FOOD (inference). Free NVIDIA models when the wallet is
# empty; frontier models unlock automatically once USDC lands in the wallet.
#
# What this script DOES (and does NOT) do:
#   1. ensures a self-owned wallet at ~/.automaton/wallet.json (auto-gen, never a human key)
#   2. starts runtime/compute-proxy/proxy.mjs on :18402 (x402 self-pay, OpenAI-compatible)
#   3. exports OPENAI_BASE_URL + OPENAI_API_KEY (placeholder) + ANICCA_MODEL so an
#      OpenAI-compatible loop you pass in routes inference through the self-paid proxy
#   4. exec "$@" if you give it a loop command; otherwise HOLD the proxy in the
#      foreground and PRINT how to plug your loop in.
#
# Anicca SHIPS its loop at runtime/loop/index.mjs. Pass it as the loop command:
#   ./start-local.sh node runtime/loop/index.mjs
# With NO args this script only holds the self-pay proxy in the foreground and
# prints how to start the loop. Any OpenAI-compatible loop that reads
# OPENAI_BASE_URL / OPENAI_API_KEY (or ANICCA_MODEL) routes through the proxy.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE="${LIFE_MANAGER_NODE:-$(command -v node || true)}"
case "$NODE" in
  /*) [ -x "$NODE" ] || { echo "[local] managed node is not executable: $NODE" >&2; exit 127; } ;;
  *) echo "[local] LIFE_MANAGER_NODE must resolve to an absolute executable" >&2; exit 127 ;;
esac
export LIFE_MANAGER_NODE="$NODE"
PORT="${COMPUTE_PROXY_PORT:-18402}"
PROXY_ONLY=0
if [ "${1:-}" = "--proxy-only" ]; then
  PROXY_ONLY=1
  shift
fi
# per-instance EVM identity (#26 EQUALIZE, R4): the wallet path is resolved by a single sourced
# helper (unit-tested) so every instance gets its OWN isolated wallet under its OWN $ANICCA_HOME,
# while ONLY the original default-home instance keeps the legacy shared $HOME wallet. A spawn with
# a different $ANICCA_HOME never inherits another instance's key (see resolve-wallet-path.sh).
# shellcheck source=resolve-wallet-path.sh
. "$HERE/resolve-wallet-path.sh"
WALLET="$(resolve_wallet_path)"

# --- 1. self-owned wallet (no human key) -------------------------------
if [ ! -f "$WALLET" ]; then
  mkdir -p "$(dirname "$WALLET")"
  WALLET_PATH="$WALLET" "$NODE" -e '
    const {generatePrivateKey,privateKeyToAccount}=require("'"$HERE"'/node_modules/viem/accounts");
    const fs=require("fs"); const pk=generatePrivateKey(); const wp=process.env.WALLET_PATH;
    fs.writeFileSync(wp,
      JSON.stringify({privateKey:pk,address:privateKeyToAccount(pk).address},null,2));
    fs.chmodSync(wp,0o600);
    console.error("[local] created self-owned wallet "+privateKeyToAccount(pk).address);
  '
else
  echo "[local] wallet preserved: $(WALLET_PATH="$WALLET" "$NODE" -e 'console.log(JSON.parse(require("fs").readFileSync(process.env.WALLET_PATH)).address)')"
fi

# --- 1b. self-owned Solana wallet (fund via Binance SOL -> auto-swap to USDC) --
# Lets anyone top anicca up from Binance (which in many regions only sells SOL):
# send SOL here, the funding daemon swaps it to USDC on Base. No human key, no deps.
if [ "$PROXY_ONLY" -eq 0 ]; then
  "$NODE" "$HERE/ensure-solana-wallet.mjs" || echo "[local] WARN: solana wallet gen skipped"
fi

# --- 1c. born-with-Polygon (#27 EQUALIZE): self-owned Polymarket deposit wallet --
# The moment this instance has its OWN EVM key (step 1 above), it can also own a
# Polymarket CLOB V2 deposit wallet — deployed GASLESS via Polymarket's relayer (the
# EOA only signs a SIWE message; no funds needed or moved). This just grants the
# ADDRESS; funding it (pUSD / bridged USDC) is a separate, later, deliberate step —
# see skills/earn/polymarket-trade/SKILL.md. Best-effort / non-blocking: any missing
# python/pip/network never fails wallet creation (see ensure-polymarket-deposit-wallet.sh).
if [ "$PROXY_ONLY" -eq 0 ]; then
  EFFECTIVE_HOME="${ANICCA_HOME:-$HOME/.anicca}"
  bash "$HERE/ensure-polymarket-deposit-wallet.sh" "$WALLET" "$EFFECTIVE_HOME" \
    || echo "[local] WARN: Polymarket deposit-wallet bootstrap skipped (non-blocking)"
fi

# --- 2. start the x402 self-pay proxy ----------------------------------
# /models is a process-readiness probe only. Provider inference is verified separately without
# charging or fabricating a completion during ordinary daemon boot.
if [ "$PROXY_ONLY" -eq 1 ]; then
  if curl -sS --max-time 2 "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then
    echo "[local] compute-proxy already live on :$PORT"
    exit 0
  fi
  echo "[local] starting compute-proxy on :$PORT (x402 self-pay from own wallet)..."
  cd "$HERE"
  exec env -u ANICCA_EVM_PRIVATE_KEY -u BLOCKRUN_WALLET_KEY -u PKVAR -u BASE_CHAIN_WALLET_KEY \
    COMPUTE_PROXY_PORT="$PORT" "$NODE" proxy.mjs
fi

PROXY_PID=""
if ! curl -sS --max-time 2 "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; then
  echo "[local] starting compute-proxy on :$PORT (x402 self-pay from own wallet)..."
  ( cd "$HERE" && env -u ANICCA_EVM_PRIVATE_KEY -u BLOCKRUN_WALLET_KEY -u PKVAR -u BASE_CHAIN_WALLET_KEY \
      COMPUTE_PROXY_PORT="$PORT" "$NODE" proxy.mjs ) &
  PROXY_PID="$!"
  # wait for the HTTP server to bind (port up). Live inference still needs the
  # network + a free-tier/funded wallet — see step 4 / verify notes.
  for _ in $(seq 1 20); do
    curl -sS --max-time 1 "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1 && break
    sleep 0.5
  done
else
  echo "[local] compute-proxy already live on :$PORT"
fi

# --- 3. point any OpenAI-compatible loop at the self-paid proxy --------
export OPENAI_BASE_URL="http://127.0.0.1:$PORT/v1"
export OPENAI_API_KEY="${OPENAI_API_KEY:-x402-local-nokey}"   # placeholder; proxy pays in USDC, not this
# The repository proxy maps this profile to its configured concrete frontier model.
export ANICCA_MODEL="${ANICCA_MODEL:-auto}"
# The loop requires ANICCA_HOME (no default) — derive it the same way install.sh does.
export ANICCA_HOME="${ANICCA_HOME:-$HOME/.anicca}"
# Expose the self-owned wallet address so the loop can read its balance (tier selection).
# Derive from the private key when the wallet file has no `address` field (older format).
export ANICCA_WALLET_ADDRESS="${ANICCA_WALLET_ADDRESS:-$(WALLET_PATH="$WALLET" "$NODE" -e '
  try {
    const w=JSON.parse(require("fs").readFileSync(process.env.WALLET_PATH));
    if (w.address) { console.log(w.address); }
    else if (w.privateKey) {
      const {privateKeyToAccount}=require("'"$HERE"'/node_modules/viem/accounts");
      console.log(privateKeyToAccount(w.privateKey).address);
    }
  } catch(e){ process.exit(0); }
')}"
echo "[local] inference -> $OPENAI_BASE_URL  model=$ANICCA_MODEL  (repository x402 proxy)"
echo "[local] ANICCA_HOME=$ANICCA_HOME  wallet=$ANICCA_WALLET_ADDRESS"

# --- 4. run loop or hold ------------------------------------------------
if [ "$#" -gt 0 ]; then
  echo "[local] launching your loop: $*"
  exec "$@"
else
  echo "[local] no loop command given — holding the self-pay compute proxy in foreground."
  echo "[local] start the anicca loop with:"
  echo "[local]     ./start-local.sh node runtime/loop/index.mjs   (from the repo root)"
  echo "[local] (or attach any OpenAI-compatible loop that reads OPENAI_BASE_URL=$OPENAI_BASE_URL)"
  echo "[local] fund frontier: send USDC to the wallet address above; your loop can then pick a paid model."
  echo "[local] (Ctrl-C to stop the proxy.)"
  wait
fi
