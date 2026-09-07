#!/usr/bin/env bash
# cook/run.sh — the EXPLORE earner: search the web for a NEW way to earn, surface candidates, and
# (when the model decides) share the find to the colony forum so peers can try it too.
#
# HARD RULE #0: this is a TOOL, not a decision. cook does NOT decide WHAT to build or WHETHER a lead is
# good — it brings the model fresh, real candidates (with URLs) and the model decides. Pattern copied
# from vinid/einstein-arena (explore → try → share-numbers) + BlockRunAI/Franklin (the model judges from
# real context, no hardcoded seed list). NOTHING hardcoded: the search query comes from $ANICCA_ARGS
# (the model's curiosity this wake) or a small rotating default; we do not pin specific repos/links.
#
# Env: ANICCA_ARGS (JSON, optional) — {"query":"<what to look for>"}; WAKE_ID.
# Output: records ONE narrate line to the earn ledger (source=cook) + prints the candidates as JSON so
# the model sees them next wake. Never spends money, never bricks.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WAKE="${WAKE_ID:-$(date -u +%s)}"
EARN_STATE_ROOT="${EARN_STATE_ROOT:-${LIFE_MANAGER_SKILLS_STATE_ROOT:-${ANICCA_HOME:-$HOME/.local/state/life-manager}/state/skills}/earn}"
LEDGER="${EARN_LEDGER:-$EARN_STATE_ROOT/earn-ledger.jsonl}"
mkdir -p "$(dirname "$LEDGER")"

# the model's search intent this wake (its own words), or a neutral seed that rotates by hour. Resolved
# by a PURE, tested module — the old `${ANICCA_ARGS:-{}}` bash default appended a stray `}` and corrupted
# the model's query to invalid JSON, so its curiosity was ALWAYS dropped to a seed (fixed 2026-06-22).
QUERY=$(node "$HERE/lib/resolve-query.mjs" "${ANICCA_ARGS:-}")
echo "[cook] exploring: $QUERY"

# Use firecrawl's SEARCH API — scraping google/ddg directly is bot-blocked and returned ZERO candidates
# (system bug found 2026-06-22, 22 wakes × 0 results). `firecrawl search` returns real result URLs.
RESULTS=$(/opt/homebrew/bin/firecrawl search "$QUERY earn USDC agent github" 2>/dev/null \
  | grep -oE "https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|https://[a-z0-9.-]+\.[a-z]{2,}/[^ )]*" \
  | grep -viE "google|/search|duckduckgo" | sort -u | head -6)
[ -z "$RESULTS" ] && RESULTS="(no fresh candidates this wake; try a different query)"
echo "[cook] candidates:"; printf '%s\n' "$RESULTS" | sed 's/^/  - /'

# record a narrate line so the wake is logged + the candidates ride into the next wake's context.
CANDJSON=$(printf '%s' "$RESULTS" | python3 -c "import sys,json;print(json.dumps([l for l in sys.stdin.read().split('\n') if l.strip()]))")
JSON=$(python3 -c "import json;print(json.dumps({'source':'cook','task':'explore: ${QUERY//\'/}','candidates':$CANDJSON,'earn_usdc':0,'cost_usdc':0,'wake':'$WAKE'}))" 2>/dev/null)
env -u GOOGLE_LOGIN_PASSWORD -u GOOGLE_LOGIN_EMAIL -u COMPOSIO_API_KEY -u COMPOSIO_AUTH_TOKEN \
  node "$HERE/../earn/lib/record.mjs" "$JSON" "$LEDGER" 2>/dev/null || echo "$JSON" >> "$LEDGER"
echo "[cook] recorded explore wake (candidates surfaced for the model to try next)."
exit 0
