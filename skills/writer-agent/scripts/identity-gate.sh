#!/usr/bin/env bash
# identity-gate.sh — IDENTITY gate (spec docs/loop-engineering/47-writer-loop-quality-and-self-improvement.md
# §1 row 6 + §2, Dais 裁定 2026-07-18): an article must never present its author as an AI ("私はAI", "自律型の
# AI", "Mac mini の中で"...), and must never state an unverified track-record claim ("毎日記事にしています"
# while the article is still a draft). Two-layer judgment, same shape as deslop-gate.sh/eval-gate.sh:
#   (1) deterministic pre-check — known AI self-disclosure phrases, mechanical, zero tolerance
#   (2) LLM judge — everything a fixed phrase list cannot catch: paraphrased self-disclosure, an
#       inflated/unverified track-record claim, or internal context (Dais by name, spec filenames,
#       private host paths) leaking into the body
# Usage: identity-gate.sh <article.md> [--lang ja|en]
# stdout: one JSON line {"verdict":"PASS|FAIL","violations":[...]} ; exit 0 only on PASS.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ARTICLE_ROOT="${ARTICLE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh" || exit $?
MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-$ARTICLE_ROOT/runtime/model-runner.sh}"

MD=""; LANG_A="ja"
POSITIONAL=()
while [ $# -gt 0 ]; do case "$1" in
  --lang) LANG_A="$2"; shift 2;;
  --markdown-file) MD="$2"; shift 2;;   # accepted for consistency with the other gates
  *) POSITIONAL+=("$1"); shift;;
esac; done
[ -z "$MD" ] && [ ${#POSITIONAL[@]} -gt 0 ] && MD="${POSITIONAL[0]}"
[ -f "$MD" ] || { echo "FATAL: usage: identity-gate.sh <article.md> [--lang ja|en]" >&2; exit 2; }

GATES_LOG="${ARTICLE_GATES_LOG:-$WRITER_LOG_DIR/article-gates.log}"
log_gate_verdict() {
  mkdir -p "$(dirname "$GATES_LOG")" 2>/dev/null || return 0
  printf '%s script=identity-gate.sh md=%s lang=%s verdict=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$MD" "$LANG_A" "$1" >>"$GATES_LOG" 2>/dev/null || true
}

# ---- (1) DETERMINISTIC PRE-CHECK -- known AI self-disclosure phrases, zero tolerance ----
MECH=()
BODY="$(cat "$MD")"
# The judge reviews reader-visible prose, not transport metadata. Keep BODY intact for the
# deterministic safety scan, then remove frontmatter and the canonical media publishing
# envelope before asking the model to reason about identity/internal-context leakage.
JUDGE_BODY="$(printf '%s' "$BODY" | python3 -c '
import re, sys, urllib.parse
text = sys.stdin.read()
if text.startswith("---\n"):
    text = re.sub(r"\A---\n.*?\n---\n?", "", text, count=1, flags=re.S)
text = re.sub(
    r"(?ms)^<!-- canonical-media:start -->\n.*?^<!-- canonical-media:end -->\n?",
    "",
    text,
)
text = re.sub(
    r"(?m)^<!-- canonical-media: (?:headline-image|body-diagram)\.png -->\n?",
    "",
    text,
)
text = re.sub(
    r"(?m)^<!-- mermaid-source: body-diagram\.mmd -->\n?",
    "",
    text,
)
# Revenue attribution is mandatory public transport metadata, validated
# separately by cta-gate.sh against the current run/artifact. Hide only a
# complete owned CTA query from the identity judge; it is not prose and
# otherwise conflicts structurally with the CTA lineage contract. Other
# hosts, partial queries, paths, and reader-visible link text stay intact.
required = {"product_id", "run_id", "artifact_id", "variant_id", "click_id"}
pattern = re.compile(
    r"https://(?:www\.)?aniccaai\.com/[^\s)\]>\x22\x27]*"
)
def project_owned_cta(match):
    url = match.group(0)
    parsed = urllib.parse.urlsplit(url)
    keys = set(urllib.parse.parse_qs(parsed.query, keep_blank_values=True))
    if not required.issubset(keys):
        return url
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, "", parsed.fragment)
    )
text = pattern.sub(project_owned_cta, text)
sys.stdout.write(text)
')"
# JP phrasings (real miss, spec §1 row 6: "私はアニッチャ...自律型のAIです...Mac miniの中で...毎日、
# 自分が実際に触ったものだけを検証して記事にしています").
JP_HITS=$(printf '%s' "$BODY" | grep -n -E '私(は|が).{0,20}(AI|ＡＩ)|自律型の?(AI|ＡＩ)|Mac ?mini の中で|としてのAIである|AIとして(の)?私|自分（アニッチャ）は|エージェントである私' || true)
if [ -n "$JP_HITS" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    MECH+=("AI self-disclosure (JP pattern) -- $line")
  done <<< "$JP_HITS"
fi
# EN phrasings
EN_HITS=$(printf '%s' "$BODY" | grep -n -i -E "i am an? (autonomous )?ai\b|as an ai[, ]|i'?m an ai\b|autonomous ai (agent|entity|writer)|running inside a mac mini|i live inside a mac mini" || true)
if [ -n "$EN_HITS" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    MECH+=("AI self-disclosure (EN pattern) -- $line")
  done <<< "$EN_HITS"
fi

if [ ${#MECH[@]} -gt 0 ]; then
  MECH_JSON=$(python3 -c "
import json, sys
print(json.dumps(sys.argv[1:]))
" "${MECH[@]}")
  echo "{\"verdict\":\"FAIL\",\"violations\":$MECH_JSON}"
  log_gate_verdict "FAIL:mechanical"
  exit 1
fi

# ---- (2) LLM JUDGE -- paraphrased self-disclosure / inflated track record / internal-context leak ----
PROMPT="You are a strict identity/honesty reviewer for a publishable article. A deterministic pre-check has
ALREADY verified there is no literal AI self-disclosure phrase (\"I am an AI\", \"自律型のAI\", \"Mac miniの中で\"
etc). Do not look for those; they are handled. Judge only what a fixed phrase list cannot catch.

Read the article below and find, if any:
(A) SELF-DISCLOSURE (paraphrased) -- any sentence, in any wording, that identifies the author/narrator as an
    AI, a bot, an autonomous agent, a program, or otherwise not-a-human -- even indirectly (e.g. describing
    itself as \"running\", \"executing\", \"a process\", \"an instance\", or naming its own hardware/software
    environment as if it were the narrator's home).
(B) UNVERIFIED TRACK-RECORD CLAIM -- a claim of an established habit/output/audience (\"毎日記事にしています\",
    \"I publish daily\", \"read by thousands\", \"N人が読んでいます\") that the article itself gives no evidence
    for, especially when the article's own content indicates it is a first/early piece (e.g. still a draft, no
    prior citation of past output).
(C) INTERNAL-CONTEXT LEAK -- a person's private/internal name (e.g. \"Dais\"), an internal spec/document
    filename, an internal-only path (e.g. /home/<user>/<private-runtime>), or any other detail that only makes
    sense to someone inside the writing team, not to an external reader.
    A public HTTPS URL in the final Sources block is an external reference, not an internal-context leak.
    Do not classify a public repository URL as C merely because its URL path names files or directories.

For each hit, quote the offending sentence and say which category (A/B/C) it is.
Output the FINAL LINE as pure JSON exactly:
{\"verdict\":\"PASS\"|\"FAIL\",\"violations\":[\"A|B|C: <quoted text>\", ...]}
verdict = FAIL if there is ONE OR MORE hit in any category. Otherwise PASS.

=== ARTICLE (lang=$LANG_A) ===
$JUDGE_BODY"

# Judge broker routing must survive agent subshells that drop env vars
# (observed 2026-07-25): the markdown lives in the run dir, so derive
# ARTICLE_RUN_DIR from it whenever the caller failed to pass one.
if [ -z "${ARTICLE_RUN_DIR:-}" ] && [ -f "$MD" ]; then
  export ARTICLE_RUN_DIR="$(cd "$(dirname "$MD")" && pwd)"
fi
OUT=$(printf '%s' "$PROMPT" | "$MODEL_RUNNER" judge --prompt-file - 2>/dev/null)
JSON=$(printf '%s\n' "$OUT" | grep -o '{"verdict".*}' | tail -1)
if [ -z "$JSON" ]; then
  log_gate_verdict "FATAL:no-json-from-judge"
  echo "FATAL: judge returned no JSON verdict" >&2
  printf '%s\n' "$OUT" | tail -5 >&2
  exit 3
fi
echo "$JSON"
JUDGE_VERDICT=$(printf '%s' "$JSON" | grep -o '"verdict":"[A-Z]*"' | head -1 | cut -d'"' -f4)
if [ -n "${ARTICLE_RUN_DIR:-}" ]; then
  mkdir -p "$ARTICLE_RUN_DIR/gates"
  ARTICLE_HASH=$(shasum -a 256 "$MD" | awk '{print $1}')
  RECEIPT=$(printf '%s' "$JSON" | "${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}" -c "
import json, sys
payload = json.load(sys.stdin)
payload['article_sha256'] = sys.argv[1]
print(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))
" "$ARTICLE_HASH")
  printf '%s\n' "$RECEIPT" > "$ARTICLE_RUN_DIR/gates/identity-$LANG_A.json"
fi
log_gate_verdict "${JUDGE_VERDICT:-UNKNOWN}"
printf '%s' "$JSON" | grep -q '"verdict":"PASS"'
