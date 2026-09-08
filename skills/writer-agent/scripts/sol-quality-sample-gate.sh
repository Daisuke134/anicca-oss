#!/usr/bin/env bash
# One deterministic, persisted Sol calibration audit after Terra PASS.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROL="${ARTICLE_SOL_TRIGGER_CONTROL:-$DIR/sol_trigger_control.py}"
MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-${ARTICLE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}/runtime/model-runner.sh}"
MD="${1:-}"
LANG_A="${2:-}"

[ -f "$MD" ] || { echo "FATAL: Sol sample article is missing" >&2; exit 2; }
case "$LANG_A" in ja|en) ;; *) echo "FATAL: Sol sample language must be ja or en" >&2; exit 2 ;; esac

# Unmanaged/manual editorial checks have no durable run identity and are not
# calibration candidates. Production and recovery wrappers always export both.
if [ -z "${ARTICLE_RUN_ID:-}" ] || [ -z "${ARTICLE_RUN_DIR:-}" ]; then
  exit 0
fi

STATE="${ARTICLE_SOL_SAMPLE_STATE:-${ARTICLE_STATE_DIR:-${ARTICLE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}/state}/sol-quality-sample.json}"
GATES="$ARTICLE_RUN_DIR/gates"
TRIGGER="$GATES/sol-trigger-$LANG_A.json"
AUDIT="$GATES/sol-audit-$LANG_A.json"
ARTICLE_HASH="$(shasum -a 256 "$MD" | awk '{print $1}')"
mkdir -p "$GATES"

CONTROL_OUT="$(python3 "$CONTROL" quality-sample --state "$STATE" \
  --run-id "$ARTICLE_RUN_ID" --artifact-id "article-$LANG_A" --article "$MD" \
  --language "$LANG_A" --receipt "$TRIGGER")" || {
    echo "FATAL: Sol sample control failed" >&2
    exit 3
  }
CONTROL_STATUS="$(printf '%s' "$CONTROL_OUT" | jq -r '.status // "ERROR"')"

case "$CONTROL_STATUS" in
  NOT_SAMPLED|LANGUAGE_PENDING) exit 0 ;;
  ALREADY_BOUND)
    BOUND_HASH="$(printf '%s' "$CONTROL_OUT" | jq -r '.bound_article_sha256 // empty')"
    if [ ! -f "$AUDIT" ] || ! jq -e --arg hash "$BOUND_HASH" \
      '.article_sha256 == $hash and (.verdict == "PASS" or .verdict == "FAIL")' \
      "$AUDIT" >/dev/null 2>&1; then
      echo "FATAL: sampled Sol audit was bound but has no durable verdict" >&2
      exit 3
    fi
    # The prior sampled bytes were audited. A changed repair returns to the
    # ordinary Terra boundary and never buys a second calibration call.
    exit 0
    ;;
  RECEIPT_READY) ;;
  *) echo "FATAL: unexpected Sol sample status: $CONTROL_STATUS" >&2; exit 3 ;;
esac

if [ -f "$AUDIT" ]; then
  if ! jq -e --arg hash "$ARTICLE_HASH" \
    '.article_sha256 == $hash and (.verdict == "PASS" or .verdict == "FAIL")' \
    "$AUDIT" >/dev/null 2>&1; then
    echo "FATAL: current Sol audit receipt conflicts with sampled bytes" >&2
    exit 3
  fi
  cat "$AUDIT"
  [ "$(jq -r '.verdict' "$AUDIT")" = "PASS" ] && exit 0
  exit 1
fi
if [ -d "$TRIGGER.claim" ]; then
  echo "FATAL: Sol sample receipt was claimed without a durable verdict" >&2
  exit 3
fi

ARTICLE="$(cat "$MD")"
PROMPT="You are the fresh calibration editor for one already-Terra-reviewed article.
Find any material publish-blocking defect the first editor missed. Judge evidence,
reader usefulness, honest framing, native ${LANG_A} language, and the single relevant
measurable CTA. Do not score or rewrite the article.

Return exactly one JSON object:
{\"verdict\":\"PASS\"|\"FAIL\",\"fixes\":[\"bounded concrete fix\"],\"strengths\":[\"preserve this\"]}

=== ARTICLE (${LANG_A}) ===
${ARTICLE}"

set +e
OUT="$(printf '%s' "$PROMPT" | ARTICLE_MODEL_ROLE=sol-audit \
  ARTICLE_SOL_TRIGGER_RECEIPT="$TRIGGER" "$MODEL_RUNNER" judge --prompt-file - 2>/dev/null)"
MODEL_RC=$?
set -e
if [ "$MODEL_RC" -ne 0 ]; then
  echo "FATAL: sampled Sol provider failed rc=$MODEL_RC" >&2
  exit 3
fi
JSON="$(printf '%s\n' "$OUT" | grep -o '{"verdict".*}' | tail -1)"
if [ -z "$JSON" ]; then
  echo "FATAL: sampled Sol audit returned no JSON verdict" >&2
  exit 3
fi

RECEIPT="$(printf '%s' "$JSON" | python3 -c '
import json,sys
p=json.load(sys.stdin)
if p.get("verdict") not in {"PASS","FAIL"}: raise SystemExit(2)
p["article_sha256"]=sys.argv[1]
p["trigger"]="quality_sample"
p["model"]="gpt-5.6-sol"
p["requested_reasoning_effort"]="medium"
print(json.dumps(p,ensure_ascii=False,separators=(",",":")))
' "$ARTICLE_HASH")" || { echo "FATAL: invalid sampled Sol verdict" >&2; exit 3; }
TMP_AUDIT="$GATES/.sol-audit-$LANG_A.$$.tmp"
printf '%s\n' "$RECEIPT" >"$TMP_AUDIT"
mv "$TMP_AUDIT" "$AUDIT"
printf '%s\n' "$RECEIPT"
[ "$(printf '%s' "$RECEIPT" | jq -r '.verdict')" = "PASS" ] && exit 0
exit 1
