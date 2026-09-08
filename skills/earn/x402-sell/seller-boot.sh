#!/usr/bin/env bash
# seller-boot.sh — per-instance x402 seller entrypoint for a KeepAlive supervisor (launchd plist
# written by ../run.sh strategy=x402). Sources facilitator creds then exec's the seller.
# Env from the plist: X402_PAYTO, X402_PORT, X402_PUBLIC_URL, OPENCLAW_ENV_FILE(optional).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
# Skill code executes directly from the immutable repository release, so this directory owns both
# serve.mjs and its locked dependencies. Fail closed if the release dependency tree is incomplete.
[ -d "$DIR/node_modules/@coinbase/x402" ] || {
  echo "locked x402 dependencies missing from immutable release: $DIR" >&2
  exit 78
}
source "$DIR/runtime-env.sh"
exec /usr/bin/env node "$DIR/serve.mjs"
