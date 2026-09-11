#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-${HOME}/.local/state/life-manager/.env}"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/load-env-file.sh"
lm_load_env_file "$ENV_FILE"
source "$SCRIPT_DIR/lib/portable-runtime.sh"
lm_prepare_portable_runtime "$REPO_ROOT"
exec "$LM_PYTHON" "$LM_TIMEOUT_RUNNER" 300 "$LM_NODE" "$SCRIPT_DIR/tiktok-metrics-due.js"
