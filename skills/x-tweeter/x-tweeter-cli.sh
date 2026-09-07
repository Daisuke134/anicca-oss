#!/usr/bin/env bash
set -uo pipefail

SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export X_LOOP_ROLE=original
export X_LOOP_NAME=x-tweeter
export X_REPOST_BROWSER_IDENTITY="x:anicca"
export X_REPOST_MODEL="gpt-5.6-luna"
export X_REPOST_REASONING_EFFORT="max"
export X_REPOST_PUBLISH_TRANSPORT="postiz"
export X_REPOST_POSTIZ_INTEGRATION_ID="cmt4l2jld031tqp0y8qtyo983"
export X_REPOST_FORCE_KIND=original
export X_REPOST_FORCE_LANGUAGE=en
export X_REPOST_SOURCE_MODE=chinese-public
export X_REPOST_DISABLE_AFFILIATE=1
export X_REPOST_STATE_DIR="${X_TWEETER_STATE_DIR:-$HOME/loops/x-tweeter}"
export AFFILIATE_REPOST_PROPOSAL_PATH="$X_REPOST_STATE_DIR/no-affiliate-proposal.json"
export AFFILIATE_X_DISTRIBUTION_QUEUE="$X_REPOST_STATE_DIR/no-affiliate-jobs.jsonl"

PY=/opt/homebrew/bin/python3; [ -x "$PY" ] || PY=python3
CANDIDATES="${X_REPOST_CANDIDATES_FILE:-$X_REPOST_STATE_DIR/chinese-candidates-latest.json}"
if [ -z "${X_REPOST_CANDIDATES_FILE:-}" ]; then
  mkdir -p "$X_REPOST_STATE_DIR"
  if ! "$PY" - "$CANDIDATES" "${X_TWEETER_CANDIDATE_MAX_AGE_SECONDS:-3300}" <<'PYEOF'
import json, pathlib, sys, time
path, max_age = pathlib.Path(sys.argv[1]), int(sys.argv[2])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
    valid = int(value.get("candidate_count", 0)) > 0 and time.time() - path.stat().st_mtime <= max_age
except (OSError, ValueError, TypeError):
    valid = False
raise SystemExit(0 if valid else 1)
PYEOF
  then
    TEMPORARY="$(mktemp "$X_REPOST_STATE_DIR/.chinese-candidates.XXXXXX")" || exit 1
    trap 'rm -f "$TEMPORARY"' EXIT
    "$PY" "$SKILL/scripts/chinese_source_collect.py" \
      --query-file "$SKILL/config/chinese-queries.txt" \
      --observed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --limit 7 >"$TEMPORARY" || exit 1
    mv "$TEMPORARY" "$CANDIDATES" || exit 1
    trap - EXIT
  fi
fi
export X_REPOST_CANDIDATES_FILE="$CANDIDATES"

exec "$SKILL/../x-repost/x-repost-cli.sh" "$@"
