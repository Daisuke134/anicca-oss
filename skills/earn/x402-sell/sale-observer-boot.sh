#!/bin/bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
. "$DIR/runtime-env.sh"
exec /usr/bin/env node "$DIR/sale-observer.mjs"
