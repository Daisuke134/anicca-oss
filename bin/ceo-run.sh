#!/usr/bin/env bash
# ceo-run.sh — REQ-CEO-008/REQ-CEO-015: the CEO runner. Three modes:
#
#   bash ceo-run.sh --light-pass
#       Daily deterministic pass: bin/budget-check.sh for every registry loop. NEVER spawns the
#       weekly agent-judgment route (PROP-077).
#
#   bash ceo-run.sh --apply-decision <payload.json>
#       Deterministic write gate: validates an allocation-decision payload and, only if valid,
#       atomically writes config/loop-registry.json + appends ledgers/ceo-decisions.jsonl.
#
#   bash ceo-run.sh   (no args)
#       Compact weekly evaluation: builds a deterministic aggregate snapshot, skips without an
#       agent when evidence is insufficient, otherwise uses the shared provider-agnostic runner
#       and applies its structured output through the deterministic write gate.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=/opt/homebrew/bin/python3; [ -x "$PY" ] || PY=python3
BASE="${CEO_STATE_DIR:-$HERE}"

case "${1:-}" in
  --light-pass)
    "$PY" "$HERE/bin/ceo_run.py" light-pass "$BASE"
    exit $?
    ;;
  --apply-decision)
    PAYLOAD="${2:-}"
    if [ -z "$PAYLOAD" ]; then
      echo "usage: ceo-run.sh --apply-decision <payload.json>" >&2
      exit 1
    fi
    "$PY" "$HERE/bin/ceo_run.py" apply-decision "$BASE" "$PAYLOAD"
    exit $?
    ;;
  --record-pass)
    SUMMARY="${2:-}"
    if [ -z "$SUMMARY" ]; then
      echo "usage: ceo-run.sh --record-pass <summary.json>" >&2
      exit 1
    fi
    "$PY" "$HERE/bin/ceo_run.py" record-pass "$BASE" "$SUMMARY"
    exit $?
    ;;
  --apply-evaluation)
    EVALUATION="${2:-}"
    if [ -z "$EVALUATION" ]; then
      echo "usage: ceo-run.sh --apply-evaluation <evaluation.json>" >&2
      exit 1
    fi
    "$PY" "$HERE/bin/ceo_run.py" apply-evaluation "$BASE" "$EVALUATION"
    exit $?
    ;;
  --snapshot)
    shift
    exec "$PY" "$HERE/bin/ceo_unit_economics.py" "$BASE" "$@"
    ;;
  "")
    # true no-args invocation -- fall through to the full weekly-evaluation default mode below.
    ;;
  *)
    # FIND-003 (fresh-adversary, sprint-1): any unrecognized/malformed $1 (typo, --help, a stray
    # flag) must NEVER fall through to the weekly evaluation branch below. Reject explicitly.
    echo "usage: ceo-run.sh [--light-pass|--snapshot [--date YYYY-MM-DD]|--apply-decision <payload.json>|--record-pass <summary.json>]" >&2
    echo "ceo-run.sh: unrecognized argument: $1" >&2
    exit 2
    ;;
esac

# Default mode: compact weekly evaluation. Collection and eligibility are deterministic.
# Every weekly path records a pass whether or not an allocation changes: the deterministic skip
# is the --record-pass equivalent, while an accepted structured evaluation records after its gate.
mkdir -p "$BASE/ledgers" "$BASE/config"
for f in ceo-decisions lessons; do
  [ -f "$BASE/ledgers/$f.jsonl" ] || : > "$BASE/ledgers/$f.jsonl"
done

"$PY" "$HERE/bin/ceo_unit_economics.py" "$BASE" || exit $?
SNAPSHOT="$BASE/ledgers/ceo-unit-economics.latest.json"
ELIGIBLE="$($PY - "$SNAPSHOT" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("1" if d.get("mode")=="active" and any(v.get("allocation_eligible") for v in d.get("loops",{}).values()) else "0")
PY
)"

if [ "$ELIGIBLE" != "1" ]; then
  SUMMARY="$(mktemp "${TMPDIR:-/tmp}/ceo-weekly-skip.XXXXXX")"
  trap 'rm -f "$SUMMARY"' EXIT
  "$PY" - "$SNAPSHOT" "$SUMMARY" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
out={"reviewed":sorted(d.get("loops",{})),"changed":[],"reason":"observe_only_or_insufficient_verified_evidence"}
json.dump(out,open(sys.argv[2],"w"))
PY
  "$PY" "$HERE/bin/ceo_run.py" record-pass "$BASE" "$SUMMARY"
  echo "ceo-weekly: deterministic skip (observe-only or insufficient verified evidence)"
  exit 0
fi

RUN_AGENT="${CEO_RUN_AGENT_BIN:-$HERE/skills/earn/marketing-engine/run_agent.sh}"
SCHEMA="$HERE/config/ceo-weekly-evaluation.schema.json"
EVIDENCE_DIR="$BASE/ledgers/.ceo-weekly-evidence/$(date +%s)-$$"
RESULT="$(mktemp "${TMPDIR:-/tmp}/ceo-weekly-result.XXXXXX")"
PROMPT="$(mktemp "${TMPDIR:-/tmp}/ceo-weekly-prompt.XXXXXX")"
trap 'rm -f "$RESULT" "$PROMPT"' EXIT
"$PY" - "$SNAPSHOT" >"$PROMPT" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
print("Review this compact seven-day CEO snapshot. Propose changes only for allocation_eligible loops. "
      "Do not read files, use tools, or perform external actions. Unknown profitability is never zero cost. "
      "Return no decision when evidence does not justify one. Snapshot:\n"+json.dumps(d,separators=(",",":")))
PY

if ! "$RUN_AGENT" --task-class repeatable-agent --evidence-dir "$EVIDENCE_DIR" \
    --task-label ceo-weekly-evaluation --loop control-plane --schema "$SCHEMA" \
    --workdir "$HERE" --print-result <"$PROMPT" >"$RESULT"; then
  echo "ceo-weekly: shared runner failed" >&2
  exit 1
fi
"$PY" "$HERE/bin/ceo_run.py" apply-evaluation "$BASE" "$RESULT"
