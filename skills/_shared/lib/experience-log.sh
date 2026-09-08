#!/usr/bin/env bash
# experience-log.sh — Anicca のその日の経験を JSONL に記録する lib
# Spec: ANICCA_USEFUL_CONTENT_SPEC.md HR-D
# Schema: $WRITER_STATE_DIR/experience-log/<YYYY-MM-DD>.jsonl
# Entry:
#   {ts, source, summary, key_quote, sentiment, useful_for_content}
# Usage:
#   . "$LIFE_MANAGER_REPO/skills/_shared/lib/experience-log.sh"
#   el_append <source> <summary> [key_quote] [sentiment=neutral] [useful_for_content=y]
#   el_today                                  # JSONL stdout
#   el_recent_days <N>                        # 直近 N 日 JSONL
#   el_useful_today                           # useful_for_content=y のみ

set -euo pipefail

EL_DIR="${WRITER_EXPERIENCE_DIR:-${WRITER_STATE_DIR:?WRITER_STATE_DIR is required}/experience-log}"

el__ensure() {
  command -v jq >/dev/null 2>&1 || { echo "el: jq missing" >&2; return 1; }
  mkdir -p "$EL_DIR" || return 1
}

el_append() {
  local source_name="$1" summary="$2" key_quote="${3:-}" sentiment="${4:-neutral}" useful="${5:-y}"
  el__ensure
  local ts date file
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  date="$(date -u +%Y-%m-%d)"
  file="$EL_DIR/${date}.jsonl"
  touch "$file"
  jq -nc \
    --arg ts "$ts" \
    --arg source "$source_name" \
    --arg summary "$summary" \
    --arg key_quote "$key_quote" \
    --arg sentiment "$sentiment" \
    --arg useful "$useful" \
    '{ts:$ts, source:$source, summary:$summary, key_quote:$key_quote, sentiment:$sentiment, useful_for_content:$useful}' \
    >> "$file"
}

el_today() {
  el__ensure
  local date file
  date="$(date -u +%Y-%m-%d)"
  file="$EL_DIR/${date}.jsonl"
  [[ -s "$file" ]] && cat "$file"
}

el_recent_days() {
  local n="${1:-7}"
  el__ensure
  local i d file
  for ((i=0; i<n; i++)); do
    if d="$(date -u -v-"${i}"d +%Y-%m-%d 2>/dev/null)"; then :; \
    else d="$(date -u -d "${i} days ago" +%Y-%m-%d)"; fi
    file="$EL_DIR/${d}.jsonl"
    [[ -s "$file" ]] && cat "$file"
  done
}

el_useful_today() {
  el__ensure
  local date file
  date="$(date -u +%Y-%m-%d)"
  file="$EL_DIR/${date}.jsonl"
  [[ -s "$file" ]] && jq -c 'select(.useful_for_content=="y")' "$file"
}

# CLI
if [[ "${BASH_SOURCE[0]:-}" == "${0}" ]]; then
  cmd="${1:-}"; shift || true
  case "$cmd" in
    append) el_append "$@" ;;
    today) el_today ;;
    recent) el_recent_days "$@" ;;
    useful) el_useful_today ;;
    *) echo "usage: experience-log.sh {append|today|recent N|useful}" >&2; exit 1 ;;
  esac
fi
