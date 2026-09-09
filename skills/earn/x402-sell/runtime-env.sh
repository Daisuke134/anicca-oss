#!/usr/bin/env bash

X402_SKILL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export X402_SKILL_DIR
export X402_STATE_DIR="${X402_STATE_DIR:-${LIFE_MANAGER_STATE_ROOT:-${HOME}/.local/state/life-manager/x402-sell}}"
mkdir -p "$X402_STATE_DIR"
X402_ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME}/.local/state/life-manager/.env}"
if [ -f "$X402_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$X402_ENV_FILE"
  set +a
fi
