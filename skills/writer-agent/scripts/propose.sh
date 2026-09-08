#!/usr/bin/env bash
# article-daily propose — thin wrapper around _shared/propose-and-rewrite.sh
# Usage: bash propose.sh --channel <zenn|devto|substack-ja|substack-en|aniccaai-blog>
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"
CHANNEL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) CHANNEL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$CHANNEL" ]] || { echo "FATAL: --channel required" >&2; exit 1; }

case "$CHANNEL" in
  zenn)            PLATFORM="Zenn"; ACCOUNT="${ZENN_ACCOUNT:?ZENN_ACCOUNT is required}"; LANG="ja" ;;
  devto)           PLATFORM="Dev.to"; ACCOUNT="${DEVTO_ACCOUNT_HANDLE:?DEVTO_ACCOUNT_HANDLE is required}"; LANG="en" ;;
  substack-ja)     PLATFORM="Substack"; ACCOUNT="${SUBSTACK_PUBLICATION_JA:-${SUBSTACK_PUBLICATION:?SUBSTACK_PUBLICATION_JA or SUBSTACK_PUBLICATION is required}}"; LANG="ja" ;;
  substack-en)     PLATFORM="Substack"; ACCOUNT="${SUBSTACK_PUBLICATION_EN:?SUBSTACK_PUBLICATION_EN is required}"; LANG="en" ;;
  note)            PLATFORM="Note"; ACCOUNT="${NOTE_URLNAME:?NOTE_URLNAME is required}"; LANG="ja" ;;
  aniccaai-blog)   PLATFORM="aniccaai-blog"; ACCOUNT="${ARTICLE_BLOG_ACCOUNT:?ARTICLE_BLOG_ACCOUNT is required}"; LANG="ja" ;;
  *) echo "FATAL: unknown channel: $CHANNEL" >&2; exit 1 ;;
esac

exec bash "$LIFE_MANAGER_REPO/skills/_shared/propose-and-rewrite.sh" \
  --channel "article-${CHANNEL}" \
  --platform "$PLATFORM" \
  --account "$ACCOUNT" \
  --lang "$LANG" \
  --candidates 5
