#!/usr/bin/env bash
# Dice AI Zero Japanese-only half-hour quote loop. Reuses the x-repost contracts and ledgers,
# but owns its browser identity, state, model home, language policy, and external-effect transport.
set -uo pipefail

STATE="${X_REPOST_JA_STATE_DIR:-$HOME/.local/state/life-manager/social-x/x-repost/ja}"
mkdir -p "$STATE"
touch "$STATE/no-affiliate-jobs.jsonl"

export X_LOOP_NAME="x-repost-ja"
export X_REPOST_STATE_DIR="$STATE"
export X_REPOST_BROWSER_IDENTITY="x:diceai0"
export X_REPOST_MODEL="gpt-5.6-luna"
export X_REPOST_REASONING_EFFORT="max"
export X_REPOST_ACCOUNT_HANDLE="@diceai0"
export X_REPOST_EXPECTED_HANDLE="diceai0"
export X_REPOST_ACCOUNT_DESCRIPTION="Life Managerを作る起業家。AI・プロダクト・深層技術・crypto・finance・build in public・お笑いを横断し、失敗と実測を短く面白く共有してファンを増やす"
export X_REPOST_FORCE_KIND="quote"
export X_REPOST_FORCE_LANGUAGE="ja"
export X_REPOST_SOURCE_LANGUAGE_POLICY="any"
export X_REPOST_QUERIES_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config/queries-ja.txt"
export X_REPOST_PUBLISH_TRANSPORT="browser"
export AFFILIATE_REPOST_PROPOSAL_PATH="$STATE/no-affiliate-proposal.json"
export AFFILIATE_X_DISTRIBUTION_QUEUE="$STATE/no-affiliate-jobs.jsonl"

exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/x-repost-cli.sh" "$@"
