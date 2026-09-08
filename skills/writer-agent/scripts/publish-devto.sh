#!/usr/bin/env bash
# publish-devto.sh — managed Dev.to boundary for Writer Agent
# Usage:
#   bash publish-devto.sh --markdown-file <f> --title <t> --meta <m>

set -euo pipefail

MD_FILE=""
TITLE=""
META=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --markdown-file) MD_FILE="$2"; shift 2 ;;
    --title)         TITLE="$2"; shift 2 ;;
    --meta)          META="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -f "$MD_FILE" ]] || { echo "FATAL: markdown not found: $MD_FILE" >&2; exit 1; }

if ! grep -q '^title:' "$MD_FILE"; then
  echo "FATAL: markdown missing 'title:' frontmatter" >&2; exit 1
fi
if ! grep -q '^tags:' "$MD_FILE"; then
  echo "FATAL: markdown missing 'tags:' frontmatter (dev.to requires tags)" >&2; exit 1
fi

# Managed exact8 runs use the tracked, ID-preserving Dev.to driver. It stages
# published:false, records the authenticated numeric article ID, and later
# flips only that ID live. There is no second unmanaged/manual pipeline.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"

if [[ -z "${ARTICLE_RUN_DIR:-}" || ! -f "${ARTICLE_PUBLICATION_STATE:-}" || ! -f "${ARTICLE_LEDGER:-}" ]]; then
  echo "FATAL: managed publication state is required" >&2
  exit 2
fi

# --- fail-closed PII gate (scripts/pii-gate.py) ---------------------------------------
# Nothing operator-identifying may reach Dev.to. ANY non-zero exit from the gate -- a finding,
# an unconfigured blocklist, or an internal scanner error -- aborts this publish. Gate output
# goes to stderr so it cannot pollute this script's single-line stdout contract.
python3 "$SCRIPT_DIR/pii-gate.py" --stage publish-devto "$MD_FILE" >&2 || exit $?

STAGED="$(python3 "$SCRIPT_DIR/devto-publish/devto.py" stage)" || exit $?
ARTICLE_ID="$(printf '%s' "$STAGED" | jq -r '.article_id // empty')"
DASHBOARD_URL="$(printf '%s' "$STAGED" | jq -r '.dashboard_url // empty')"
[[ "$ARTICLE_ID" =~ ^[0-9]+$ && -n "$DASHBOARD_URL" ]] || {
  echo "FATAL: managed Dev.to stage returned malformed evidence: $STAGED" >&2
  exit 4
}
echo "DRAFT id=$ARTICLE_ID url=$DASHBOARD_URL"
