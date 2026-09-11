#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/lib/portable-runtime.sh"
lm_prepare_portable_runtime "$REPO_ROOT"

exec "$LM_PYTHON" "$LM_TIMEOUT_RUNNER" 240 "$LM_NODE" \
  "$SCRIPT_DIR/../lib/report-job-adapter.js" enqueue "$@"
