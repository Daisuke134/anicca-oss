#!/usr/bin/env bash
# eval-gate.sh — G2+G3 gate (spec #18): a FRESH adversary scores the article /80 (spec #72,
# 2026-07-17: expanded from 5 to 8 dimensions, see below) and fact-checks it. Threshold 56
# (same 70% bar as the original 35/50). The reader persona: a smart, busy, skeptical reader
# who has seen a hundred AI-hype posts and decides whether THIS one is worth paying for.
# Usage: eval-gate.sh --markdown-file <md> --lang <ja|en> [--threshold 56] [--title <t>]
# --title (spec #72 rule 5, optional): needed for the TITLE FULFILLMENT dimension below --
# without it, that dimension is skipped (scored as N/A, not counted against the total).
# stdout: one JSON line {"score":N,"verdict":...,"fact_flags":[...],"reasons":[...],
#   "claim_count":N,"artifact_count":N,"claim_artifact_ratio_ok":bool,
#   "payment_verdict":"yes"|"no","payment_reason":"..."}
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ARTICLE_ROOT="${ARTICLE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh" || exit $?
MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-$ARTICLE_ROOT/runtime/model-runner.sh}"
MD=""; LANG_A=""; TH=56; TITLE_ARG=""
while [ $# -gt 0 ]; do case "$1" in
  --markdown-file) MD="$2"; shift 2;;
  --lang) LANG_A="$2"; shift 2;;
  --threshold) TH="$2"; shift 2;;
  --title) TITLE_ARG="$2"; shift 2;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; done
[ -f "$MD" ] && [ -n "$LANG_A" ] || { echo "FATAL: --markdown-file --lang required" >&2; exit 2; }

# Persist every verdict past this run's stdout (2026-07-17, gate-crash incident, same fix
# as deslop-gate.sh) -- one append-only line per attempt, best-effort.
# ARTICLE_GATES_LOG override (2026-07-17, incident: an ad-hoc manual verification run
# against this same default path truncated real production evidence with no backup --
# tests/callers that need an isolated log MUST set this, never write to the production
# default), defaults to the real production path.
GATES_LOG="${ARTICLE_GATES_LOG:-$WRITER_LOG_DIR/article-gates.log}"
log_gate_verdict() {
  mkdir -p "$(dirname "$GATES_LOG")" 2>/dev/null || return 0
  printf '%s script=eval-gate.sh md=%s lang=%s verdict=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$MD" "$LANG_A" "$1" >>"$GATES_LOG" 2>/dev/null || true
}

# Run evidence is owned by the gate, not by an ad-hoc caller redirection. In
# particular, the free-version payment note belongs on stderr; capturing 2>&1
# must never be able to turn gates/eval-gate-<lang>.json into invalid JSON.
persist_run_artifact() {
  local json="$1" gate_dir target tmp
  [ -n "${ARTICLE_RUN_DIR:-}" ] || return 0
  gate_dir="$ARTICLE_RUN_DIR/gates"
  target="$gate_dir/eval-gate-$LANG_A.json"
  if ! mkdir -p "$gate_dir"; then
    echo "FATAL: cannot create eval gate artifact directory: $gate_dir" >&2
    return 1
  fi
  tmp=$(mktemp "$target.tmp.XXXXXX") || {
    echo "FATAL: cannot create temporary eval gate artifact: $target" >&2
    return 1
  }
  if ! printf '%s\n' "$json" >"$tmp" || ! python3 - "$tmp" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    value = json.load(f)
if not isinstance(value, dict) or value.get('verdict') not in ('PASS', 'FAIL'):
    raise SystemExit(1)
PY
  then
    rm -f "$tmp"
    echo "FATAL: refusing to persist invalid eval gate JSON: $target" >&2
    return 1
  fi
  if ! mv -f "$tmp" "$target"; then
    rm -f "$tmp"
    echo "FATAL: cannot atomically publish eval gate artifact: $target" >&2
    return 1
  fi
}

TITLE_LINE=""
[ -n "$TITLE_ARG" ] && TITLE_LINE="=== TITLE ===
$TITLE_ARG

"

PROMPT="You are a fresh adversarial reviewer with zero attachment to this article. Grade it for a
smart, busy, skeptical reader deciding whether to PAY for the full version.

Score 8 dimensions, 0-10 each (total /80, spec #72 2026-07-17 expansion from the original 5):
1. CLAIM DISCIPLINE — every factual claim has a named source or a first-hand receipt; no vague authority
2. SUBSTANCE — the reader learns something they could not get from skimming 3 free blog posts
3. STRUCTURE — one topic per paragraph, headings state propositions honestly, no bait
4. PROSE — rhythm varies, no filler, no hype vocabulary, reads like a careful human expert
5. HONESTY — limitations and counter-evidence stated; numbers are real and checkable
6. BILLBOARD POSITION (spec #72 rule 1) — the article's core discovery/angle (the ONE thing
   worth reading this for) must appear by the END of the first third of the body (by word/
   paragraph count), not buried as a slow-build reveal at 60-70%+ through the piece. Score 10
   if the angle is stated or strongly signaled in the opening third; 0 if it only becomes
   clear in the final third.
7. TITLE FULFILLMENT (spec #72 rule 5) — does the body actually deliver what the title
   promises, with nothing the title implies left unproven? Score 10 if every promise in the
   title is backed by real content in the body; dock points for each unfulfilled promise or
   overclaim. If no TITLE section is provided below, score this dimension exactly 5 (neutral,
   does not count for or against) and say so in its reason.
8. CONCLUSION QUALITY (spec #72 rule 6) — the final section must NOT merely re-summarize what
   was already said. It needs exactly three things: (a) one genuinely NEW sentence not stated
   earlier (a synthesis, an implication, or a caveat the body did not already spell out), (b)
   a concrete pointer to what the reader should look at/verify/try NEXT, (c) one concrete
   action the reader can personally take. Score 10 if all three are clearly present; dock
   points per missing element.

HOUSE CITATION STYLE (read this before judging claim discipline — calibrated 2026-07-16):
this publication puts ALL citations in ONE block at the end (## Sources / ## 出典) and does NOT
use inline citations in the body. That is the established, deliberate format (SKILL.md rule 26),
not a defect. A number in the body with no bracket next to it is NORMAL here.
Therefore:
- A claim counts as SOURCED if the end Sources block plausibly covers it (the named project,
  protocol, or dataset appears there), OR the piece reports it as a first-hand receipt
  ('we ran it', 'we measured', our own on-chain log).
- fact_flags = claims that have NO source anywhere in the piece AND no first-hand receipt.
  Missing per-claim inline mapping is NOT a fact_flag. Do not flag a number merely because the
  citation lives at the end instead of next to it.
- If the piece has no Sources block at all and states outside numbers, THAT is a real fact_flag.

Also list fact_flags under the rule above (quote each).

CLAIM-TO-ARTIFACT RATIO (spec #72 rule 2, separate BLOCKING check, not part of the /80 score):
count every PRIMARY-OBSERVATION claim in the body -- a sentence saying the writer personally
read/ran/opened/clicked/measured something ('読んだ', '叩いた', '見た', 'ran it', 'checked the
source', 'opened the contract', etc, in ANY phrasing, not just those literal words). Separately
count every real ARTIFACT actually shown in the body backing one of those claims -- a real code
fragment, a real JSON/data excerpt, a real table, a description of a real screenshot referenced
by content (not just 'see screenshot' with nothing shown). claim_artifact_ratio_ok = true only
if artifacts >= (primary-observation claims / 3), i.e. at least one real artifact for every
three such claims. If there are zero primary-observation claims, this is trivially true (nothing
to back). Put the counts in claim_count and artifact_count.

PAYMENT VERDICT (spec #72 rule 7, separate BLOCKING check, not part of the /80 score): as the
same skeptical reader persona, answer honestly: would you personally pay ¥1,000 (ja) / \$8 (en)
for the FULL version of this article, based on what the free preview + your reading of the whole
piece show you? payment_verdict = \"yes\" or \"no\". If \"no\", payment_reason MUST name the
concrete missing thing (e.g. 'paid section has no artifact not already in the free preview',
'the promised depth never arrives', 'nothing here is worth more than a free blog post'). Also
check: for the PAID section specifically, is there at least ONE thing that exists ONLY there
(a full data table, fully annotated code, a step-by-step the free preview only teases)? If not,
that alone should drive payment_verdict to \"no\".

PROBLEM-CLASS CHECK (spec docs/loop-engineering/47-writer-loop-quality-and-self-improvement.md §1,
separate BLOCKING check, not part of the /80 score -- these are the 8 real defect classes found in a
draft that had already cleared every other gate, so check for them explicitly even on a high-scoring
piece):
1. ZERO-PRIOR-KNOWLEDGE DROP-OFF -- does paragraph 1 assume the reader already knows the central
   proper noun/product/protocol this piece is about, with no plain-language introduction of what it
   even is?
2. INTERNAL-CONTEXT LEAK -- a private person's first name (e.g. 'Dais'), an internal spec/document
   filename, or an internal-only path/tool name that only means something to the writing team
3. NO PROMISE UP FRONT -- the opening never states, in plain language, what the reader will
   understand or be able to do after reading
4. NO WHY-PAY / WHY-CARE -- the piece never makes explicit, early, why THIS reader specifically
   should spend time or money on it (stakes, not just facts)
5. CHRONOLOGICAL-DIARY STRUCTURE -- the piece follows the order the writer investigated things
   ('first I checked X, then I found Y') instead of the order the reader cares about
6. HOOK BURIED -- the single most interesting/surprising fact in the whole piece is not signalled
   by the end of the first third
7. JARGON DENSITY -- technical terms/acronyms/proper nouns appear undefined at first use (a
   katakana transliteration of an English term, e.g. バーチャルズ for Virtuals, is NOT a definition)
8. TITLE UNCLEAR TO OUTSIDER -- someone who has never heard of this topic could not tell from the
   title ALONE what subject the article is even about

For each of the 8, answer yes (problem present, quote the evidence) or no. problem_classes_clear =
true only if ALL 8 are answered no.

Report per-dimension scores with one-line reasons.
FINAL LINE must be pure JSON exactly:
{\"score\":N,\"verdict\":\"PASS\"|\"FAIL\",\"fact_flags\":[\"...\"],\"reasons\":[\"dim1: ...\",...],\"claim_count\":N,\"artifact_count\":N,\"claim_artifact_ratio_ok\":true|false,\"payment_verdict\":\"yes\"|\"no\",\"payment_reason\":\"...\",\"problem_class_hits\":[\"<n>. <class>: <quote>\",...],\"problem_classes_clear\":true|false}
verdict = PASS only if score >= $TH AND fact_flags is empty AND claim_artifact_ratio_ok is true
AND payment_verdict is \"yes\" AND problem_classes_clear is true. Any one of those failing makes it
FAIL, even with a high score.

${TITLE_LINE}=== ARTICLE (lang=$LANG_A) ===
$(cat "$MD")"

# Judge broker routing must survive agent subshells that drop env vars
# (observed 2026-07-25): the markdown lives in the run dir, so derive
# ARTICLE_RUN_DIR from it whenever the caller failed to pass one.
if [ -z "${ARTICLE_RUN_DIR:-}" ] && [ -f "$MD" ]; then
  export ARTICLE_RUN_DIR="$(cd "$(dirname "$MD")" && pwd)"
fi
OUT=$(printf '%s' "$PROMPT" | "$MODEL_RUNNER" judge --prompt-file - 2>/dev/null)
JSON=$(printf '%s\n' "$OUT" | grep -o '{"score".*}' | tail -1)
if [ -z "$JSON" ]; then
  log_gate_verdict "FATAL:no-json-from-judge"
  echo "FATAL: judge returned no JSON verdict" >&2
  printf '%s\n' "$OUT" | tail -5 >&2
  exit 3
fi

# PAYMENT_VERDICT SCOPE (2026-07-18 fix, discovered while staging a Lane-A devlog to all 5
# channels): rule 7's "would you pay for the PAID section" check only makes sense for note's
# 一部有料記事 format (SKILL.md MONETIZATION section), which marks its paid boundary with a
# 有料ライン heading/marker. zenn/devto/substack-ja/X articles are, BY HOUSE DESIGN, plain free
# pieces with no paid section at all (SKILL.md: "free version... Never hand-truncate" — they are
# not supposed to contain one) -- run.sh calls this gate identically for every channel with no
# --platform distinction, so payment_verdict was structurally guaranteed to be "no" for every
# non-note draft regardless of content quality, silently blocking 4 of 5 platforms every day.
# Fix: only enforce payment_verdict as blocking when the article itself carries a paid-section
# marker; otherwise report it (still visible in the JSON) but do not let it veto verdict.
if printf '%s\n' "$(cat "$MD")" | grep -qE '有料ライン|ここから先は有料|paid-line|PAYWALL'; then
  JUDGE_VERDICT=$(printf '%s' "$JSON" | grep -o '"verdict":"[A-Z]*"' | head -1 | cut -d'"' -f4)
  log_gate_verdict "${JUDGE_VERDICT:-UNKNOWN}"
  persist_run_artifact "$JSON" || exit 4
  echo "$JSON"
  printf '%s' "$JSON" | grep -q '"verdict":"PASS"'
else
  SCORE=$(printf '%s' "$JSON" | grep -o '"score":[0-9]*' | head -1 | grep -o '[0-9]*')
  FACT_FLAGS_EMPTY=0; printf '%s' "$JSON" | grep -q '"fact_flags":\[\]' && FACT_FLAGS_EMPTY=1
  RATIO_OK=0; printf '%s' "$JSON" | grep -q '"claim_artifact_ratio_ok":true' && RATIO_OK=1
  # PROBLEM-CLASS CHECK (spec 47 §1, Unit 6): same additive AND as the two checks above -- a
  # free-version draft can score high and still hit one of the 8 real defect classes.
  PROBLEM_CLASSES_CLEAR=0; printf '%s' "$JSON" | grep -q '"problem_classes_clear":true' && PROBLEM_CLASSES_CLEAR=1
  FREE_PASS=0
  if [ -n "$SCORE" ] && [ "$SCORE" -ge "$TH" ] && [ "$FACT_FLAGS_EMPTY" -eq 1 ] && [ "$RATIO_OK" -eq 1 ] && [ "$PROBLEM_CLASSES_CLEAR" -eq 1 ]; then
    FREE_PASS=1
  fi
  if [ "$FREE_PASS" -eq 1 ]; then
    JSON=$(printf '%s' "$JSON" | sed -E 's/"verdict":"FAIL"/"verdict":"PASS"/')
  fi
  persist_run_artifact "$JSON" || exit 4
  echo "$JSON"
  if [ "$FREE_PASS" -eq 1 ]; then
    echo "note: payment_verdict is advisory-only here — no paid-section marker found, this is a free-version draft" >&2
  fi
  log_gate_verdict "$([ "$FREE_PASS" -eq 1 ] && echo PASS || echo FAIL)(free-version)"
  [ "$FREE_PASS" -eq 1 ]
fi
