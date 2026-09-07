#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${LIFE_MANAGER_REPO:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

# Agent state is user data and remains outside the immutable source release.
export ANICCA_HOME="${DAILY_NL_AGENT_HOME:-$HOME/.anicca}"
export REPORT_TO="${REPORT_TO:-contact@aniccaai.com}"
NODE=$(command -v node || echo /opt/homebrew/bin/node)
exec "$NODE" "$REPO_ROOT/skills/report/daily-nl-report.mjs"
