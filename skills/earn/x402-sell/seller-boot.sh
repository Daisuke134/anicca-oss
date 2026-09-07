#!/usr/bin/env bash
# seller-boot.sh — per-instance x402 seller entrypoint for a KeepAlive supervisor (launchd plist
# written by ../run.sh strategy=x402). Sources facilitator creds then exec's the seller.
# Env from the plist: X402_PAYTO, X402_PORT, X402_PUBLIC_URL, OPENCLAW_ENV_FILE(optional).
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
# runtime/self-update-skills.sh rsyncs repo/skills → ANICCA_HOME/skills with --exclude='node_modules'
# (right call: 635M per instance would fill the disk). So this dir has serve.mjs but no dependencies,
# and node dies on ERR_MODULE_NOT_FOUND '@coinbase/x402' before binding the port — which is why every
# loop-spawned seller was in a crash loop (measured 2026-07-16: runs=213/168/213, all exit 1). The
# serve.mjs files are byte-identical, so exec the copy that HAS the dependency tree.
[ -d "$DIR/node_modules/@coinbase/x402" ] || {
  echo "locked x402 dependencies missing from immutable release: $DIR" >&2
  exit 78
}
source "$DIR/runtime-env.sh"
exec /usr/bin/env node "$DIR/serve.mjs"
