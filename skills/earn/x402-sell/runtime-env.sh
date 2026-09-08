#!/usr/bin/env bash

X402_SKILL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export X402_SKILL_DIR
X402_ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME}/.local/state/life-manager/.env}"
if [ -f "$X402_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$X402_ENV_FILE"
  set +a
fi
