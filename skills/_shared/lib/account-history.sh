#!/usr/bin/env bash
# account-history.sh — per (channel, platform, account) post history with 14d anti-repeat
# Spec: ANICCA_USEFUL_CONTENT_SPEC.md HR-C
# Schema: $WRITER_STATE_DIR/content-library/account-history.jsonl
# Entry:
#   {ts, channel, platform, account, hook_hash, structure_hash, pattern_id, content_snippet, status}
# Usage:
#   . "$LIFE_MANAGER_REPO/skills/_shared/lib/account-history.sh"
#   ah_record   <channel> <platform> <account> <hook> <structure_type> <pattern_id> <snippet> [status=posted]
#   ah_check    <channel> <platform> <account> <hook> <structure_type> [days=14]   # exit 0 if new, exit 2 if repeat
#   ah_list     <channel> <platform> <account> [days=14]                            # JSONL on stdout
#
# fail-closed: if jq missing -> exit 1. if history file path unwritable -> exit 1.

set -euo pipefail

AH_FILE="${WRITER_CONTENT_LIBRARY_DIR:-${WRITER_STATE_DIR:?WRITER_STATE_DIR is required}/content-library}/account-history.jsonl"

ah__hash() {
  printf '%s' "$1" | shasum -a 1 | awk '{print substr($1,1,12)}'
}

ah__ensure_file() {
  command -v jq >/dev/null 2>&1 || { echo "ah: jq missing" >&2; return 1; }
  mkdir -p "$(dirname "$AH_FILE")" || return 1
  [[ -f "$AH_FILE" ]] || : > "$AH_FILE"
}

ah_record() {
  local channel="$1" platform="$2" account="$3" hook="$4" structure_type="$5" pattern_id="$6" snippet="${7:-}" status="${8:-posted}"
  ah__ensure_file
  local ts hook_hash structure_hash
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  hook_hash="$(ah__hash "$hook")"
  structure_hash="$(ah__hash "$structure_type")"
  jq -nc \
    --arg ts "$ts" \
    --arg channel "$channel" \
    --arg platform "$platform" \
    --arg account "$account" \
    --arg hook_hash "$hook_hash" \
    --arg structure_hash "$structure_hash" \
    --arg pattern_id "$pattern_id" \
    --arg content_snippet "$snippet" \
    --arg status "$status" \
    '{ts:$ts, channel:$channel, platform:$platform, account:$account, hook_hash:$hook_hash, structure_hash:$structure_hash, pattern_id:$pattern_id, content_snippet:$content_snippet, status:$status}' \
    >> "$AH_FILE"
  echo "$ts $channel $platform $account hook=$hook_hash struct=$structure_hash pattern=$pattern_id" >&2
}

# ah_check returns:
#   exit 0 = NEW (hook_hash + structure_hash 共に 14d 内に history 無し)
#   exit 2 = REPEAT (どちらかが 14d 内に history にある = anti-repeat HIT)
#   exit 1 = error
ah_check() {
  local channel="$1" platform="$2" account="$3" hook="$4" structure_type="$5" days="${6:-14}"
  ah__ensure_file
  local hook_hash structure_hash since
  hook_hash="$(ah__hash "$hook")"
  structure_hash="$(ah__hash "$structure_type")"
  # since = days ago in ISO Z (BSD date / GNU date 両対応)
  if since="$(date -u -v-"${days}"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"; then :; \
  else since="$(date -u -d "${days} days ago" +%Y-%m-%dT%H:%M:%SZ)"; fi
  local repeat_count
  repeat_count="$(jq -s --arg ch "$channel" --arg pf "$platform" --arg ac "$account" \
                     --arg hh "$hook_hash" --arg sh "$structure_hash" --arg since "$since" \
    'map(select(.channel==$ch and .platform==$pf and .account==$ac and .ts>=$since and (.hook_hash==$hh or .structure_hash==$sh))) | length' \
    "$AH_FILE")"
  if [[ "$repeat_count" -gt 0 ]]; then
    echo "ah_check: REPEAT hit ($repeat_count entries in last ${days}d) for $channel/$platform/$account hook=$hook_hash struct=$structure_hash since=$since" >&2
    return 2
  fi
  return 0
}

ah_list() {
  local channel="$1" platform="$2" account="$3" days="${4:-14}"
  ah__ensure_file
  local since
  if since="$(date -u -v-"${days}"d +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"; then :; \
  else since="$(date -u -d "${days} days ago" +%Y-%m-%dT%H:%M:%SZ)"; fi
  jq -c --arg ch "$channel" --arg pf "$platform" --arg ac "$account" --arg since "$since" \
    'select(.channel==$ch and .platform==$pf and .account==$ac and .ts>=$since)' \
    "$AH_FILE"
}

# CLI fallback (so callers can use either source-in or exec)
if [[ "${BASH_SOURCE[0]:-}" == "${0}" ]]; then
  cmd="${1:-}"; shift || true
  case "$cmd" in
    record) ah_record "$@" ;;
    check)  ah_check "$@" ;;
    list)   ah_list "$@" ;;
    *) echo "usage: account-history.sh {record|check|list} ..." >&2; exit 1 ;;
  esac
fi
