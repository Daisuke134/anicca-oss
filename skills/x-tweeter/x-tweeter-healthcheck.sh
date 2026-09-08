#!/usr/bin/env bash
set -uo pipefail

SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export X_LOOP_NAME=x-tweeter
export X_LOOP_LABEL=ai.anicca.x-tweeter-pass
export X_REPOST_STATE_DIR="${X_TWEETER_STATE_DIR:-$HOME/.local/state/life-manager/social-x/x-tweeter}"
export X_LOOP_INITIAL_GRACE_SECONDS="${X_TWEETER_INITIAL_GRACE_SECONDS:-3600}"

exec "$SKILL/../x-repost/x-repost-healthcheck.sh" "$@"
