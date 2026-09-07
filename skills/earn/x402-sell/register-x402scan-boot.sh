#!/usr/bin/env bash
# register-x402scan-boot.sh — run x402scan SIWX registration from a dependency-complete copy.
# Runtime skill sync deliberately excludes node_modules; on local instances the canonical repo
# retains @x402/extensions, so use that copy exactly as seller-boot-v2.sh already does for serving.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
[ -d "$DIR/node_modules/@x402/extensions" ] || {
  echo "locked x402 dependencies missing from immutable release: $DIR" >&2
  exit 78
}
exec /usr/bin/env node "$DIR/register-x402scan.mjs"
