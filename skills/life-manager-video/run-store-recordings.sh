#!/bin/bash
set -a; . "${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}" 2>/dev/null; set +a
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$HERE/store-recordings.py"
