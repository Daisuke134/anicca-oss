#!/usr/bin/env bash
# score-latest-run.sh — connect the daily run to the scorer.
#
# The pieces existed but nothing called them: the daily prompt writes
# title-candidates-<lang>.json into a run directory, and beat_rate.py has to
# be handed that file by name. Measured 2026-07-27: no caller anywhere in the
# tree. Without this the loop would keep producing ledgers that nobody scores,
# which looks exactly like working and learns exactly nothing.
#
# Runs after publication, never before: scoring is measurement and must not sit
# between a draft and its destinations (spec 47, the PUBLISH ANYWAY boundary).
#
#   score-latest-run.sh [--run-dir DIR] [--opponents N] [--max-calls N]
#
# Exit 0 when at least one language scored, 1 when there was nothing to score.
# A judge failure is never fatal here: a night without a measurement is a
# missing data point, not a broken loop.
set -uo pipefail

SKILL_DIR="${ARTICLE_SKILL_DIR:-${ARTICLE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}}"
PY="${ARTICLE_PYTHON:-${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}}"

RUN_DIR=""
OPPONENTS=5
MAX_CALLS=120

while [ $# -gt 0 ]; do case "$1" in
  --run-dir)    RUN_DIR="${2:-}"; shift 2 ;;
  --opponents)  OPPONENTS="${2:-5}"; shift 2 ;;
  --max-calls)  MAX_CALLS="${2:-120}"; shift 2 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac; done

# Newest run directory that actually carries a ledger. Newest-overall is the
# wrong pick: a run that crashed before STEP 3 has a directory and no ledger,
# and silently scoring nothing would read as success.
if [ -z "$RUN_DIR" ]; then
  RUN_DIR="$(ls -dt "$SKILL_DIR"/state/runs/*/ 2>/dev/null | while read -r d; do
    if ls "$d"gates/title-candidates-*.json >/dev/null 2>&1; then printf '%s\n' "${d%/}"; break; fi
  done)"
fi

if [ -z "$RUN_DIR" ] || [ ! -d "$RUN_DIR" ]; then
  echo '{"ok":false,"reason":"no run directory carries a title-candidates ledger"}'
  exit 1
fi

SCORED=0
for LEDGER in "$RUN_DIR"/gates/title-candidates-*.json; do
  [ -f "$LEDGER" ] || continue
  LANG_A="$(basename "$LEDGER" .json)"; LANG_A="${LANG_A##*-}"
  RUN_ID="$(basename "$RUN_DIR")"
  RECEIPT="$RUN_DIR/gates/beat-rate-$LANG_A.json"
  if [ -f "$RECEIPT" ] && [ "$RECEIPT" -nt "$LEDGER" ] \
      && jq -e --arg run "$RUN_ID" --arg lang "$LANG_A" \
        '.run_id == $run and .lang == $lang and .partial == false
         and (.calls_used | type == "number")' \
        "$RECEIPT" >/dev/null 2>&1; then
    echo "{\"ok\":true,\"lang\":\"$LANG_A\",\"status\":\"already-scored\",\"path\":\"$RECEIPT\"}"
    SCORED=$((SCORED + 1))
    continue
  fi
  if "$PY" "$SKILL_DIR/scripts/beat_rate.py" score \
      --candidates "$LEDGER" --run-dir "$RUN_DIR" \
      --opponents "$OPPONENTS" --max-calls "$MAX_CALLS"; then
    SCORED=$((SCORED + 1))
  else
    echo "{\"ok\":false,\"lang\":\"$LANG_A\",\"reason\":\"beat_rate did not complete\"}" >&2
  fi
done

if [ "$SCORED" -eq 0 ]; then
  echo '{"ok":false,"reason":"ledgers present but none scored"}'
  exit 1
fi

# Blame only accumulates from scored runs, so it follows rather than leads.
"$PY" "$SKILL_DIR/scripts/rule_blame.py" collect --state-root "$SKILL_DIR/state/runs" >/dev/null 2>&1 || true
"$PY" "$SKILL_DIR/scripts/beat_rate.py" report --run-dir "$RUN_DIR" 2>/dev/null || true
exit 0
