#!/usr/bin/env bash
# editorial-gate.sh — ONE composite editorial judge replacing the four
# separate quality judges (rubric, reader-testing, deslop, eval).
#
# Why one judge: four specialised judges pulled the same draft in different
# directions and quality DEGRADED across revisions (rubric 70 -> 62 -> 49 on
# 2026-07-25), the documented self-correction failure mode
# (arxiv.org/abs/2310.01798). The sources we copy from — Anthropic's
# evaluator-optimizer pattern and hamelsmu/evals-skills — both use a single
# evaluator returning a BINARY verdict plus prioritised fixes, never a
# multi-judge score committee.
#
# Contract: same IO shape as the gates it replaces.
#   editorial-gate.sh <article.md> [--lang ja|en]
#   stdout: {"verdict":"PASS"|"FAIL","fixes":[...],"strengths":[...]}
#   exit 0 = PASS, 1 = FAIL (FAIL blocks publication until current bytes pass),
#   3 = the judge returned no JSON (infrastructure failure, never a fake PASS)
#
# Safety gates (identity, conscience) stay SEPARATE and keep their
# fail-closed authority — quality and safety are never mixed.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-${ARTICLE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}/runtime/model-runner.sh}"
CTA_GATE="${ARTICLE_CTA_GATE:-$DIR/cta-gate.sh}"
SOL_SAMPLE_GATE="${ARTICLE_SOL_SAMPLE_GATE:-$DIR/sol-quality-sample-gate.sh}"
GATES_LOG="${ARTICLE_GATES_LOG:-}"

MD=""; LANG_A="ja"
while [ $# -gt 0 ]; do case "$1" in
  --lang) LANG_A="${2:-ja}"; shift 2 ;;
  *) MD="$1"; shift ;;
esac; done
[ -f "$MD" ] || { echo "FATAL: usage: editorial-gate.sh <article.md> [--lang ja|en]" >&2; exit 2; }
case "$LANG_A" in
  ja) FEEDBACK_LANGUAGE="Japanese" ;;
  en) FEEDBACK_LANGUAGE="English" ;;
  *) echo "FATAL: --lang must be ja or en" >&2; exit 2 ;;
esac

# Some managed callers historically exported the run's gates directory rather
# than a filename. Preserve that useful intent and choose the canonical log
# inside it; never try to append bytes to a directory.
if [ -n "$GATES_LOG" ] && [ -d "$GATES_LOG" ]; then
  GATES_LOG="$GATES_LOG/editorial-gates.log"
fi

# Judge broker routing must survive agent subshells that drop env vars.
if [ -z "${ARTICLE_RUN_DIR:-}" ] && [ -f "$MD" ]; then
  export ARTICLE_RUN_DIR="$(cd "$(dirname "$MD")" && pwd)"
fi

log_gate_verdict() {
  [ -n "$GATES_LOG" ] || return 0
  mkdir -p "$(dirname "$GATES_LOG")" 2>/dev/null || return 0
  printf '%s script=editorial-gate.sh md=%s lang=%s verdict=%s\n' \
    "$(date '+%F %T')" "$MD" "$LANG_A" "$1" >>"$GATES_LOG" 2>/dev/null || true
}

owner_is_ancestor() {
  local child="$1" target="$2" parent n
  for ((n = 0; n < 64; n++)); do
    [ "$child" = "$target" ] && return 0
    [ "$child" = 1 ] && return 1
    parent="$(ps -p "$child" -o ppid= 2>/dev/null | tr -d '[:space:]')"
    [[ "$parent" =~ ^[1-9][0-9]*$ ]] || return 1
    [ "$parent" = "$child" ] && return 1
    child="$parent"
  done
  return 1
}

ARTICLE="$(cat "$MD")"
ARTICLE_HASH=$(shasum -a 256 "$MD" | awk '{print $1}')
REQUESTED_REASONING_EFFORT="medium"
if [ -n "${ARTICLE_RUN_DIR:-}" ]; then
  PREVIOUS_RECEIPT="$ARTICLE_RUN_DIR/gates/editorial-$LANG_A.json"
  if [ -f "$PREVIOUS_RECEIPT" ]; then
    PREVIOUS_VERDICT=$(jq -r '.verdict // empty' "$PREVIOUS_RECEIPT" 2>/dev/null)
    PREVIOUS_HASH=$(jq -r '.article_sha256 // empty' "$PREVIOUS_RECEIPT" 2>/dev/null)
    PREVIOUS_EFFORT=$(jq -r '.requested_reasoning_effort // "medium"' "$PREVIOUS_RECEIPT" 2>/dev/null)
    if [ "$PREVIOUS_HASH" = "$ARTICLE_HASH" ]; then
      if [ "$PREVIOUS_VERDICT" = "FAIL" ]; then
        log_gate_verdict "BLOCK:revision-required-before-rejudge"
        echo "FATAL: revision-required-before-rejudge: editorial FAIL already exists for article_sha256=$ARTICLE_HASH" >&2
        exit 76
      fi
      if [ "$PREVIOUS_VERDICT" = "PASS" ]; then
        cat "$PREVIOUS_RECEIPT"
        log_gate_verdict "PASS:replayed-current-hash"
        if [ -x "$SOL_SAMPLE_GATE" ]; then
          bash "$SOL_SAMPLE_GATE" "$MD" "$LANG_A"
          exit $?
        fi
        exit 0
      fi
    fi
    if [ "$PREVIOUS_HASH" != "$ARTICLE_HASH" ] && [ "$PREVIOUS_EFFORT" = "high" ]; then
      REROUTE_RECEIPT="$ARTICLE_RUN_DIR/gates/quality-self-heal.json"
      AUTHORIZED_REROUTE=0
      EXPECTED_MD="$ARTICLE_RUN_DIR/article-$LANG_A.md"
      REPAIR_RUN_PHYSICAL=""
      if [ ! -L "$ARTICLE_RUN_DIR" ] && [ -d "$ARTICLE_RUN_DIR" ]; then
        REPAIR_RUN_PHYSICAL="$(cd "$ARTICLE_RUN_DIR" 2>/dev/null && pwd -P)" || REPAIR_RUN_PHYSICAL=""
      fi
      REPAIR_MD_PHYSICAL=""
      if [ -f "$MD" ] && [ ! -L "$MD" ]; then
        REPAIR_MD_DIR="$(cd "$(dirname "$MD")" 2>/dev/null && pwd -P)" || REPAIR_MD_DIR=""
        [ -n "$REPAIR_MD_DIR" ] && REPAIR_MD_PHYSICAL="$REPAIR_MD_DIR/$(basename "$MD")"
      fi
      if [ "$REPAIR_RUN_PHYSICAL" = "$ARTICLE_RUN_DIR" ] \
        && [ -f "$REROUTE_RECEIPT" ] && [ ! -L "$REROUTE_RECEIPT" ] && jq -e \
        --arg lang "$LANG_A" --arg hash "$ARTICLE_HASH" \
        --arg run_id "$(basename "$ARTICLE_RUN_DIR")" \
        'type == "object" and .version == 2 and .run_id == $run_id
         and .attempt == 2 and .action == "evaluate_reroute"
         and (.quality | type) == "object"
         and .quality[$lang].article_sha256 == $hash' \
        "$REROUTE_RECEIPT" >/dev/null 2>&1; then
        AUTHORIZED_REROUTE=1
      fi
      if [ "$AUTHORIZED_REROUTE" -ne 1 ] \
        && [ ! -e "$REROUTE_RECEIPT" ] && [ ! -L "$REROUTE_RECEIPT" ] \
        && [ "${ARTICLE_QUALITY_REPAIR_ACTIVE:-0}" = "1" ] \
        && [[ "${ARTICLE_QUALITY_REPAIR_OWNER_PID:-}" =~ ^[1-9][0-9]*$ ]] \
        && [ "$REPAIR_RUN_PHYSICAL" = "$ARTICLE_RUN_DIR" ] \
        && [ "$REPAIR_MD_PHYSICAL" = "$EXPECTED_MD" ] \
        && [ -f "$ARTICLE_RUN_DIR/gates/quality-repair-state.json" ] \
        && [ ! -L "$ARTICLE_RUN_DIR/gates/quality-repair-state.json" ] \
        && jq -e \
          --arg run_id "$(basename "$ARTICLE_RUN_DIR")" \
          --argjson owner "${ARTICLE_QUALITY_REPAIR_OWNER_PID}" \
          'type == "object" and .version == 1 and .status == "invoking"
           and .run_id == $run_id and .quality_action == "evaluate_reroute"
           and (.owner_pid | type) == "number"
           and (.owner_pid | floor) == .owner_pid and .owner_pid == $owner' \
          "$ARTICLE_RUN_DIR/gates/quality-repair-state.json" >/dev/null 2>&1 \
        && kill -0 "$ARTICLE_QUALITY_REPAIR_OWNER_PID" 2>/dev/null \
        && owner_is_ancestor "$$" "$ARTICLE_QUALITY_REPAIR_OWNER_PID"; then
        AUTHORIZED_REROUTE=1
      fi
      if [ "$AUTHORIZED_REROUTE" -ne 1 ]; then
        log_gate_verdict "BLOCK:high-escalation-exhausted"
        echo "FATAL: high-escalation-exhausted: editorial high evaluation already consumed for this language/run" >&2
        exit 77
      fi
      REQUESTED_REASONING_EFFORT="high"
    elif [ "$PREVIOUS_VERDICT" = "FAIL" ] && [ "$PREVIOUS_HASH" != "$ARTICLE_HASH" ]; then
      REQUESTED_REASONING_EFFORT="high"
    fi
  fi
fi

PROMPT="You are the single editor of record for this article. Decide ONE thing:
is it ready to publish as it stands?

Answer with a binary verdict and, when it fails, the smallest set of fixes
that would flip it. Do not score, do not rate axes, do not hedge.

Judge against this checklist, which merges what four separate judges used to
check. A failure on ANY line is a FAIL:

1. WRITTEN FOR ONE READER: a specific person with a specific problem can act
   on this. Not a survey, not an audience-shaped blur.
2. MEASURED, NOT CLAIMED: every factual assertion is something the author
   actually ran, read or measured, with the number or quote present. No
   invented benchmarks, no fabricated track record.
3. NO SLOP: no filler openings, no 'in today's fast-paced world', no
   restating the title as a conclusion, no hollow transitions, no padding
   that survives deletion without loss. The mandatory revenue-path CTA below
   is not padding merely because the informational conclusion survives without
   it.
4. HONEST SHAPE: the promise made in the title and lead is the promise the
   body delivers; limitations and failures appear where a reader would need
   them, not buried.
5. USABLE STRUCTURE: a reader skimming headings alone learns the finding;
   code, tables and figures each earn their place.
6. LANGUAGE FIT: written natively in the target language (${LANG_A}), not
   translated-sounding, terminology consistent.
7. REVENUE PATH: exactly one measurable product CTA is a mandatory
   revenue-path invariant. Judge whether it follows naturally from this
   reader's job and the article's evidence. If it does not, require a local
   rewrite that makes it relevant. Never require deleting the sole measurable
   CTA; only additional or misleading promotion may be removed.
8. CITATION HOUSE STYLE: all URLs stay in one final Sources block. A factual
   claim may name its source in prose while the matching URL remains in that
   final block. Do not fail an otherwise supported claim merely because it has
   no inline URL. Still require a short quote, concrete number, or first-hand
   receipt when the claim needs evidence; a bare URL list does not manufacture
   missing evidence.

Return EXACTLY one JSON object and nothing else:
{\"verdict\":\"PASS\"|\"FAIL\",\"fixes\":[\"...\"],\"strengths\":[\"...\"]}
- fixes: ordered by impact, each one concrete and locally applicable. Empty
  when the verdict is PASS.
- strengths: at most three, so a revision does not destroy what works.
- Write every item in fixes and strengths in ${FEEDBACK_LANGUAGE}, matching the
  article under review. Keep the JSON keys in English.

=== ARTICLE (${LANG_A}) ===
${ARTICLE}"

if [ "$REQUESTED_REASONING_EFFORT" = "high" ] && [ -n "${ARTICLE_RUN_DIR:-}" ]; then
  HIGH_CLAIM_ROOT="$ARTICLE_RUN_DIR/gates/.attempts/editorial-high-$LANG_A"
  HIGH_CLAIM="$HIGH_CLAIM_ROOT/$ARTICLE_HASH"
  mkdir -p "$HIGH_CLAIM_ROOT"
  if ! mkdir "$HIGH_CLAIM" 2>/dev/null; then
    log_gate_verdict "BLOCK:high-escalation-exhausted"
    echo "FATAL: high-escalation-exhausted: editorial high evaluation already claimed for article_sha256=$ARTICLE_HASH" >&2
    exit 77
  fi
  printf '{"version":1,"language":"%s","article_sha256":"%s","reasoning_effort":"high","status":"claimed"}\n' \
    "$LANG_A" "$ARTICLE_HASH" >"$HIGH_CLAIM/claim.json"
fi

OUT=$(printf '%s' "$PROMPT" | ARTICLE_MODEL_REASONING_EFFORT="$REQUESTED_REASONING_EFFORT" \
  "$MODEL_RUNNER" judge --prompt-file - 2>/dev/null)
JSON=$(printf '%s\n' "$OUT" | grep -o '{"verdict".*}' | tail -1)
if [ -z "$JSON" ]; then
  log_gate_verdict "FATAL:no-json-from-judge"
  echo "FATAL: editorial judge returned no JSON verdict" >&2
  printf '%s\n' "$OUT" | tail -5 >&2
  exit 3
fi

# The deterministic CTA gate and the model editor must not issue mutually
# exclusive instructions. Keep a FAIL as a FAIL, but rewrite any request to
# delete the required CTA into a request to make that CTA locally relevant.
# This preserves editorial authority without making the revenue path
# impossible to satisfy.
HAS_VALID_CTA=0
if [ -x "$CTA_GATE" ] && bash "$CTA_GATE" "$MD" >/dev/null 2>&1; then
  HAS_VALID_CTA=1
fi
JSON=$(printf '%s' "$JSON" | "${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}" -c '
import json
import re
import sys

payload = json.load(sys.stdin)
if payload.get("verdict") == "FAIL":
    reconciliations = []
    removal = re.compile(
        r"(delete|remove|cut|omit|drop|eliminate|strip|"
        r"削除|消す|取り除|省く|外す)",
        re.IGNORECASE,
    )
    revenue_path = re.compile(
        r"(cta|call[- ]?to[- ]?action|promo|promotion|promotional|"
        r"product|link|宣伝|プロダクト|商品|リンク|導線|誘導)",
        re.IGNORECASE,
    )
    replacement = (
        "Keep exactly one mandatory measurable product CTA. Rewrite it so "
        "it follows naturally from the reader job and article evidence; "
        "remove only additional or misleading promotion."
    )
    citation_demand = re.compile(
        r"(inline|in[- ]text|本文中の直接出典|本文中.{0,12}出典|"
        r"各(?:事実|主張).{0,12}出典)",
        re.IGNORECASE,
    )
    final_sources = re.compile(
        r"(末尾.{0,12}出典|出典一覧|final sources|sources block|"
        r"reference list)",
        re.IGNORECASE,
    )
    insufficient = re.compile(
        r"(不十分|insufficient|not enough|directly link|結び付け)",
        re.IGNORECASE,
    )
    citation_replacement = (
        "Keep URLs in the single final Sources block. Name the source in "
        "prose and add a short quote or concrete number where needed; do "
        "not add inline URLs."
    )
    fixes = []
    for value in payload.get("fixes", []):
        fix = str(value)
        if (
            sys.argv[1] == "1"
            and removal.search(fix)
            and revenue_path.search(fix)
        ):
            fix = replacement
            if "mandatory_measurable_cta_preserved" not in reconciliations:
                reconciliations.append("mandatory_measurable_cta_preserved")
        if (
            citation_demand.search(fix)
            and final_sources.search(fix)
            and insufficient.search(fix)
        ):
            fix = citation_replacement
            if "final_sources_block_preserved" not in reconciliations:
                reconciliations.append("final_sources_block_preserved")
        if fix not in fixes:
            fixes.append(fix)
    payload["fixes"] = fixes
    if reconciliations:
        payload["editorial_contract_reconciled"] = reconciliations
print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
' "$HAS_VALID_CTA")

VERDICT=$(printf '%s' "$JSON" | "${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}" -c "
import json, sys
print(json.loads(sys.stdin.read()).get('verdict', 'FAIL'))
" 2>/dev/null || echo FAIL)
printf '%s\n' "$JSON"
# The nightly learner reads a verdict file, not stdout. When the old rubric
# judge was replaced its JSON stopped being written and self_improve_control
# went blind ("no JA/EN quality baseline is available", 2026-07-26), so the
# replacement writes the file the learner reads.
if [ -n "${ARTICLE_RUN_DIR:-}" ]; then
  mkdir -p "$ARTICLE_RUN_DIR/gates"
  RECEIPT=$(printf '%s' "$JSON" | "${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}" -c "
import json, sys
payload = json.load(sys.stdin)
payload['article_sha256'] = sys.argv[1]
payload['requested_reasoning_effort'] = sys.argv[2]
print(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
" "$ARTICLE_HASH" "$REQUESTED_REASONING_EFFORT")
  printf '%s\n' "$RECEIPT" > "$ARTICLE_RUN_DIR/gates/editorial-$LANG_A.json"
fi
log_gate_verdict "$VERDICT"
[ "$VERDICT" = "PASS" ] || exit 1
if [ -x "$SOL_SAMPLE_GATE" ]; then
  bash "$SOL_SAMPLE_GATE" "$MD" "$LANG_A"
  exit $?
fi
exit 0
