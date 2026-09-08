#!/usr/bin/env bash
# seller-boot-v2.sh — per-instance x402 seller entrypoint for a loop-owned KeepAlive supervisor
# (launchd plist written by ../run.sh strategy=x402 action=ensure), v2 protocol variant.
# Same immutable-release dependency contract as seller-boot.sh, but execs
# serve-v2.mjs (@x402/express@2.17.0) instead of the v1 serve.mjs: SELF-STORE-1 (2026-07-18) points
# every loop-owned seller at the same v2 protocol the hand-made per-instance boot scripts already
# use (serve-franklin1-boot.sh / serve-franklin2-boot.sh / serve-claude-p-boot.sh).
# Env from the plist: X402_PAYTO, X402_PORT, X402_PUBLIC_URL, LIFE_MANAGER_ENV_FILE(optional).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
[ -d "$DIR/node_modules/@coinbase/x402" ] || {
  echo "locked x402 dependencies missing from immutable release: $DIR" >&2
  exit 78
}
source "$DIR/runtime-env.sh"
exec /usr/bin/env node "$DIR/serve-v2.mjs"
