#!/usr/bin/env bash
# Sunday 03:00 evidence-bound language whitelist learning.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"
SKILL_DIR="$WRITER_ROOT"
exec python3 "$SKILL_DIR/scripts/learn_whitelist.py" \
  --skill-dir "$SKILL_DIR"
