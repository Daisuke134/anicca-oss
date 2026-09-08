#!/usr/bin/env bash
set -uo pipefail

SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="${X_REPOST_STATE_DIR:-$HOME/.local/state/life-manager/social-x/x-repost/en}"
mkdir -p "$STATE"
touch "$STATE/no-affiliate-jobs.jsonl"

export X_REPOST_BROWSER_IDENTITY="x:anicca"
export X_REPOST_MODEL="gpt-5.6-luna"
export X_REPOST_REASONING_EFFORT="max"
export X_REPOST_PUBLISH_TRANSPORT="postiz"
export X_REPOST_POSTIZ_INTEGRATION_ID="cmt4l2jld031tqp0y8qtyo983"
export X_REPOST_FORCE_KIND="quote"
export X_REPOST_FORCE_LANGUAGE="en"
export X_REPOST_DISABLE_AFFILIATE=1
export X_REPOST_STATE_DIR="$STATE"
export AFFILIATE_REPOST_PROPOSAL_PATH="$STATE/no-affiliate-proposal.json"
export AFFILIATE_X_DISTRIBUTION_QUEUE="$STATE/no-affiliate-jobs.jsonl"

exec "$SKILL/x-repost-cli.sh" "$@"
