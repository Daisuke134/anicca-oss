#!/bin/bash
# F3 — what the staged launchd job runs daily. SAFE BY DESIGN (hardened after VSDD adversary review):
#  - ★ NOTE_FORCE_DRAFT=1 is exported here = a DETERMINISTIC gate: publish.py/toggle-plan.py refuse to click
#    投稿/更新/公開 under normal operation. (Honest scope: a fully-compromised skip-perms agent is
#    out-of-scope per the spec threat model; mitigated by input sanitization + trusted prompt + future
#    container isolation.) The "tap" to allow a manual publish = `publish-to-note.sh enable-publish` + NOTE_MODE=go.
#  - Without NOTE_TOPIC it is a pure no-op (keeps the note channel clean — only the Automaton article).
#  - NOTE_TOPIC is sanitized (allow-list + length cap) before it ever reaches the agent prompt (no injection).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/../writer-runtime-env.sh"
export NOTE_FORCE_DRAFT=1          # deterministic publish-prevention for the scheduled path
rm -f "$NOTE_WORK_ROOT/.PUBLISH_ENABLED" 2>/dev/null  # ensure no stale publish-enable sentinel
STAMP="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo now)"

RAW_TOPIC="${NOTE_TOPIC:-}"
if [ -z "$RAW_TOPIC" ]; then
  echo "$STAMP daily-run: no NOTE_TOPIC → no-op (channel kept clean). FORCE_DRAFT=1. Wire a topic source to enable drafting."
  exit 0
fi

# FIND-004: validate NOTE_TOPIC against an allow-list (JP/EN letters, digits, spaces, tiny safe punctuation).
# REJECT (no-op) if ANY disallowed char is present — never pass mangled/injection text to the skip-perms agent.
CLEAN="$(printf '%s' "$RAW_TOPIC" | LC_ALL=en_US.UTF-8 tr -cd '0-9A-Za-z \-\_\.,、。ぁ-んァ-ヶ一-龯ー')"
if [ "$CLEAN" != "$RAW_TOPIC" ] || [ "${#CLEAN}" -lt 3 ] || [ "${#CLEAN}" -gt 120 ]; then
  echo "$STAMP daily-run: NOTE_TOPIC rejected (disallowed chars or bad length). no-op (no agent run)."
  exit 0
fi
TOPIC="$CLEAN"

# Draft only. NOTE_FORCE_DRAFT=1 is inherited by the agent's publish-to-note.sh calls → cannot publish.
echo "$STAMP daily-run: drafting topic=\"$TOPIC\" (FORCE_DRAFT=1, draft only, cannot publish)"
exec "$DIR/run-note-agent.sh" --topic "$TOPIC" --key new --price 500
