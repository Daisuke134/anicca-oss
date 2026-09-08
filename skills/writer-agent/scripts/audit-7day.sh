#!/usr/bin/env bash
# Sunday 22:00 exact8 public/media/language/SEO/learning audit.
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh" || exit $?
SKILL_DIR="$WRITER_ROOT"
exec "$LIFE_MANAGER_PYTHON" "$SKILL_DIR/scripts/article_weekly_audit.py" \
  --skill-dir "$SKILL_DIR" \
  --target "${TELEGRAM_TARGET_ID:-8547730585}"
