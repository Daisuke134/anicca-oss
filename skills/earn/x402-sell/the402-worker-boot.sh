#!/usr/bin/env bash
set -u
source "$(dirname "$0")/runtime-env.sh"
DIR="$X402_SKILL_DIR"
exec /usr/bin/env node "$DIR/the402-worker-daemon.mjs"
