#!/usr/bin/env bash
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
node --input-type=module -e \
  'const { resolveThe402PublicOrigin } = await import(process.argv[1]); resolveThe402PublicOrigin();' \
  "$DIR/state-paths.mjs" || {
    echo "THE402_PUBLIC_URL must be configured in ${X402_ENV_FILE} as a public HTTPS origin" >&2
    exit 2
  }
PIDS="$(lsof -ti tcp:8096 2>/dev/null || true)"
[ -n "$PIDS" ] && kill $PIDS 2>/dev/null || true
sleep 1
/opt/homebrew/bin/tailscale funnel --bg --https=443 --set-path=/webhooks/the402 http://127.0.0.1:8096/webhooks/the402 >/dev/null 2>&1 || true
exec /usr/bin/env node "$DIR/the402-server.mjs"
