#!/usr/bin/env bash
# deslop-gate.sh — G1 gate (spec #17): a FRESH model judges the article against the
# anti-slop checklist (JP: stop-ai-slop-jp or japanese-tech-writing per --doc-type, EN:
# stop-slop). Judgment lives in the model, the checklist lives in the skill files.
# Usage: deslop-gate.sh --markdown-file <md> --lang <ja|en> [--doc-type note|tech] [--title <t>] [--platform note|zenn|substack|devto]
# --doc-type (ja only, default "note", spec #57): stop-ai-slop-jp assumes a personal
# note-style voice (二人称「あなた」等), japanese-tech-writing (vendored k16shikano gist,
# see checklists/japanese-tech-writing/README.md) assumes a formal technical-writing
# voice — spec §7.4 found these two genuinely conflict on 二人称/述語強度 if applied to
# the wrong document type, so the gate now lets the caller pick instead of forcing one.
# --title / --platform (spec #72 rule 5, 2026-07-17): both optional -- the note-specific
# untranslated-English-proper-noun title check (P5 below) only runs when BOTH are given AND
# --platform is exactly "note" (zenn/devto legitimately use technical English terms in
# titles, per spec §7.60-2).
# --title (spec #77, 2026-07-17): B8 (LLM judge, below) runs whenever --title alone is given,
# on EVERY platform including zenn/devto -- this is a DIFFERENT, broader check than P5's
# mechanical proper-noun count. Real miss: the 2026-07-17 zenn-staged title kept
# "Virtualsの求人市場を覗いたら、承認はSolidityのハンコ一つで済んでいた" (2 jargon terms)
# even after P5-only (note-scoped) passed clean on the note variant of the same article --
# a lay reader with zero domain knowledge cannot parse "Virtuals" or "Solidity" from a
# title alone, on any platform. Source: plainlanguage.gov (GSA) plain-language title
# guidance -- jargon belongs in the body for the technical reader, not the headline.
# stdout: one JSON line {"verdict":"PASS|FAIL","violations":[...]} ; exit 0 only on PASS.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ARTICLE_ROOT="${ARTICLE_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh" || exit $?
MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-$ARTICLE_ROOT/runtime/model-runner.sh}"
MD=""; LANG_A=""; DOC_TYPE="note"; TITLE_ARG=""; PLATFORM_ARG=""
while [ $# -gt 0 ]; do case "$1" in
  --markdown-file) MD="$2"; shift 2;;
  --lang) LANG_A="$2"; shift 2;;
  --doc-type) DOC_TYPE="$2"; shift 2;;
  --title) TITLE_ARG="$2"; shift 2;;
  --platform) PLATFORM_ARG="$2"; shift 2;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; done
[ -f "$MD" ] && [ -n "$LANG_A" ] || { echo "FATAL: --markdown-file --lang required" >&2; exit 2; }

# Persist every verdict past this run's stdout (2026-07-17, gate-crash incident): the
# checklist-path FATAL that blocked every platform today left no durable trace of which
# passes had actually seen a gate verdict vs. a silent crash. One append-only line per
# attempt, best-effort (never breaks the actual gate flow).
# ARTICLE_GATES_LOG override (2026-07-17, incident: an ad-hoc manual verification run
# against this same default path truncated real production evidence with no backup --
# tests/callers that need an isolated log MUST set this, never write to the production
# default), defaults to the real production path.
GATES_LOG="${ARTICLE_GATES_LOG:-$WRITER_LOG_DIR/article-gates.log}"
log_gate_verdict() {
  mkdir -p "$(dirname "$GATES_LOG")" 2>/dev/null || return 0
  printf '%s script=deslop-gate.sh md=%s lang=%s verdict=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$MD" "$LANG_A" "$1" >>"$GATES_LOG" 2>/dev/null || true
}

# Checklist vendor location (spec #58 self-containment, 2026-07-17): this used to be
# hardcoded to ~/anicca-project/.claude/skills/... which FATALs the gate on any checkout
# that does not also have that other repo cloned at that exact path. Checklists now live
# vendored inside THIS repo tree (profitable-claude), with an env override for anyone who
# still wants to point elsewhere.
CHECKLISTS="${ARTICLE_CHECKLISTS_DIR:-${ARTICLE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}/checklists}"
case "$LANG_A" in
  ja)
    case "$DOC_TYPE" in
      note) SKILL="$CHECKLISTS/stop-ai-slop-jp/SKILL.md" ;;
      tech) SKILL="$CHECKLISTS/japanese-tech-writing/SKILL.md" ;;
      *) echo "FATAL: --doc-type must be note|tech for --lang ja (got: $DOC_TYPE)" >&2; exit 2 ;;
    esac
    ;;
  en) SKILL="$CHECKLISTS/stop-slop/SKILL.md" ;;
  *) echo "FATAL: --lang must be ja|en" >&2; exit 2 ;;
esac
[ -f "$SKILL" ] || { log_gate_verdict "FATAL:checklist-missing:$SKILL"; echo "FATAL: skill file missing: $SKILL (checklists dir: $CHECKLISTS)" >&2; exit 2; }

# DETERMINISTIC PRE-CHECK (2026-07-16). What a regex can decide, a model must never decide:
# the judge was passing an em dash one run and failing it the next, and a single em dash is a
# hard house rule (one is one too many) that a 3-strike threshold silently forgave. Anything
# mechanically detectable is checked here, exactly the same way every time; the model is left
# only with the judgments that actually need judgment (unearned aphorism / unsourced claim).
MECH=()
# B2: em dash used as punctuation (— or -- ). Fenced code and URLs are exempt.
STRIPPED=$(python3 - "$MD" <<'PYEOF'
import re, sys
t = open(sys.argv[1], encoding="utf-8").read()
t = re.sub(r"```.*?```", "", t, flags=re.S)      # code fences
t = re.sub(chr(96) + r"[^" + chr(96) + r"]*" + chr(96), "", t)  # inline code is not punctuation
t = re.sub(r"<!-- canonical-media:(?:start|end) -->", "", t)  # publishing envelope markers
t = re.sub(r"https?://\S+", "", t)                # URLs (hyphens are legal there)
t = re.sub(r"^\s*[-*]\s", "", t, flags=re.M)      # list bullets
print(t)
PYEOF
)
EMDASH=$(printf '%s' "$STRIPPED" | grep -c -E '—|[^-]--[^-]' || true)
[ "$EMDASH" -gt 0 ] && MECH+=("B2: em dash used $EMDASH time(s) -- house rule is zero")
# B1: hype vocabulary anywhere (heading or body)
HYPE=$(printf '%s' "$STRIPPED" | grep -c -i -E 'game.?changer|revolutionar|paradigm shift|must.?see|don.t miss out|shocking|衝撃|必見|徹底解説|神ツール' || true)
[ "$HYPE" -gt 0 ] && MECH+=("B1: hype vocabulary $HYPE hit(s)")
# B3: filler openers/closers
FILLER=$(printf '%s' "$STRIPPED" | grep -c -i -E "let'?s dive in|at the end of the day|in today'?s fast|いかがでしたか|ぜひチェック|まとめとして" || true)
[ "$FILLER" -gt 0 ] && MECH+=("B3: filler phrase $FILLER hit(s)")
# P1 (spec #53, Rule 42/52/65): a heading whose subject is the article itself ("この記事は何か" /
# "この記事でわかること"). Real miss 2026-07-17: "## 最初に：この記事は何か" PASSED the LLM judge
# because meta-heading framing was never on the B1-B5 blocking list -- it is now mechanical.
KIJI_HEADING=$(grep -c -E '^#+.*この記事' "$MD" || true)
[ "$KIJI_HEADING" -gt 0 ] && MECH+=("P1: heading references 「この記事」 $KIJI_HEADING time(s) -- Rule 42/52/65, the article never talks about itself")
# P2 (spec #53, Rule 65): known self-reference phrases ("自分（アニッチャ）は...", "としてのAIである",
# "As an AI ..."). Only the closing [8] about-us/CTA block ("## 最後に" onward) may name アニッチャ
# / talk about being an AI (Rule 14 SCOPE FIX) -- that block uses different phrasing ("私はアニッチャ
# という...", "アニッチャ というAIを作っている") which these narrower patterns do not match, verified
# against real drafts/2026-07-17+16 ja.md and a synthetic negative fixture. Real miss 2026-07-17:
# "自分（アニッチャ）は日々..." mid-article PASSED the LLM judge.
SELFREF=$(printf '%s' "$STRIPPED" | grep -c -E '(自分|私|我々|私たち)（アニッチャ）|としてのAIである|[Aa]s an AI[ ,]' || true)
[ "$SELFREF" -gt 0 ] && MECH+=("P2: self-reference 「自分（アニッチャ）」／「としてのAIである」／「As an AI」-style $SELFREF time(s) -- Rule 65, the article never talks about itself")
# P3 (spec #53, Rule 60 "figures are part of writing, not part of publishing"): every article
# needs at least one visual (Mermaid diagram, image, or table) -- text-only walls are the thing
# Rule 60 exists to prevent. Counted on the RAW $MD (not $STRIPPED, which strips fenced code
# blocks and would erase every Mermaid diagram).
VISUALS=$(( $(grep -c '```mermaid' "$MD" || true) + $(grep -c '!\[' "$MD" || true) + $(grep -c -E '^\|.*\|.*\|' "$MD" || true) ))
[ "$VISUALS" -lt 1 ] && MECH+=("P3: no visual -- article must carry at least one figure/table (SKILL.md Rule 60)")
# P4 (spec #72 rule 8, Dais 2026-07-17 夜 -- 全レーン ですます 統一, corrected from an earlier
# wrong "lane A = だ・である" ruling that bent the spec to match a bug instead of fixing the
# bug): every ja article must be written in ですます (polite) form, no lane exception. Counts
# unambiguous sentence-final markers only (だ。/である。 = plain, です。/ます。/でした。/ました。/
# ません。 = polite) on the STRIPPED body (code fences/URLs/bullets already removed above,
# headings excluded too since a short heading like "看板" carries no verb form at all). Skipped
# entirely for --lang en (no polite/plain distinction) and skipped if the body is too short to
# judge (fewer than 3 total sentence-final hits either way -- avoids false positives on tiny
# fixtures).
if [ "$LANG_A" = "ja" ]; then
  STYLE_RESULT=$(printf '%s' "$STRIPPED" | python3 -c "
import re, sys
body = '\n'.join(l for l in sys.stdin.read().split('\n') if not l.startswith('#'))
# polite (ですます) sentence-final markers -- unambiguous
polite = len(re.findall(r'(?:です|でした|ません|ましょう|ますね|ですね|ます)。', body))
# plain (だ・である) sentence-final markers -- unambiguous. だった。/した。/切った。等 the plain
# past -た。 is also plain-form, but ました。(already counted as polite above) must NOT double
# count here, so exclude any た。immediately preceded by ましだ (i.e. skip if the preceding 2
# chars are ま+し, which is exactly the ました。 case already caught by the polite pattern).
plain_da = len(re.findall(r'(?:である|だっ?た|だ)。', body))
# exclude た。 that is really the tail of ました。 or でした。 (both already counted as polite
# above -- ました requires the preceding 2 chars まし, でした requires でし)
plain_ta = len(re.findall(r'(?<!まし)(?<!でし)た。', body))
plain = plain_da + plain_ta
total = polite + plain
print(f'{polite} {plain} {total}')
")
  read -r POLITE PLAIN STYLE_TOTAL <<< "$STYLE_RESULT"
  if [ "$STYLE_TOTAL" -ge 3 ] && [ "$PLAIN" -gt 0 ]; then
    STYLE_RATIO_OK=$(python3 -c "print(1 if ($POLITE/$STYLE_TOTAL) >= 0.85 else 0)")
    if [ "$STYLE_RATIO_OK" = "0" ]; then
      MECH+=("P4: style is not ですます -- $PLAIN plain-form (だ。/である。/plain-た。) sentence-ending(s) vs $POLITE polite-form, all lanes must be ですます (SKILL.md Rule 6, spec #72 rule 8)")
    fi
  fi
fi
# P5 (spec #72 rule 5, §7.60-2): note titles must promise only what the body proves and stay
# accessible -- at most ONE untranslated English proper noun (zenn/devto allow technical
# English terms freely; this check is note-only). Real miss 2026-07-17: 「ハンコで5%、元手ゼロ
# で稼げるVirtualsの求人市場をSolidityまで読んだ」has TWO (Virtuals, Solidity). A small
# stoplist exempts ubiquitous initialisms that are effectively Japanese loanwords now (AI, IT,
# API, etc.), not genuine untranslated proper nouns.
if [ "$PLATFORM_ARG" = "note" ] && [ -n "$TITLE_ARG" ]; then
  PROPER_NOUN_COUNT=$(TITLE_ARG="$TITLE_ARG" python3 -c "
import os, re
title = os.environ['TITLE_ARG']
STOPLIST = {'AI','IT','API','SDK','URL','HTTP','HTTPS','JSON','PDF','CEO','CTO','ROI','KPI','ID','UI','UX','OS','PC','QA','FAQ'}
words = re.findall(r'[A-Za-z][A-Za-z0-9]*', title)
proper = [w for w in words if w.upper() not in STOPLIST and (w[0].isupper() or w.isupper())]
print(len(proper))
")
  [ "$PROPER_NOUN_COUNT" -gt 1 ] && MECH+=("P5: note title has $PROPER_NOUN_COUNT untranslated English proper noun(s) (max 1 for note -- zenn/devto allow technical English freely, spec #72 rule 5 / §7.60-2): $TITLE_ARG")
fi
# ADVISORY (spec #72 rule 4, 2026-07-17): a vague quantifier (過半数/多く/ほとんど/most of/
# bigger/...) used in the SAME article that also carries structured data (a markdown table or
# a JSON code fence) it could have counted instead -- never blocking (advisory only, per
# team-lead's explicit design: this is a judgment call about whether an exact number was
# available, not a mechanical certainty), surfaced by injecting into the final JSON's "advice"
# array below regardless of which exit path (mechanical FAIL or LLM judge PASS/FAIL) is taken.
ADVISORY_JSON="[]"
HAS_DATA=$(( $(grep -c -E '^\|.*\|.*\|' "$MD" || true) + $(grep -c '```json' "$MD" || true) ))
if [ "$HAS_DATA" -gt 0 ]; then
  VAGUE_HITS=$(printf '%s' "$STRIPPED" | grep -o -E '過半数|大半|大部分|(?<!の)多く(?!の[0-9])|ほとんど|most of|a lot of|bigger|smaller' 2>/dev/null | sort -u | tr '\n' '|')
  # macOS/BSD grep has no PCRE lookaround -- redo with plain -E (portable) if the above yielded
  # nothing because of the unsupported (?<!...) syntax rather than a genuine absence of hits.
  [ -z "$VAGUE_HITS" ] && VAGUE_HITS=$(printf '%s' "$STRIPPED" | grep -o -E '過半数|大半|大部分|多く|ほとんど|most of|a lot of|bigger|smaller' | sort -u | tr '\n' '|')
  if [ -n "$VAGUE_HITS" ]; then
    ADVISORY_JSON=$(VAGUE_HITS="$VAGUE_HITS" python3 -c "
import json, os
hits = [h for h in os.environ['VAGUE_HITS'].split('|') if h]
print(json.dumps([f'P-ADV: vague quantifier \"{h}\" used despite structured data (table/JSON) elsewhere in the article -- count it exactly if the data is there (spec #72 rule 4)' for h in hits]))
")
  fi
fi
if [ ${#MECH[@]} -gt 0 ]; then
  MECH_JSON=$(python3 -c "
import json, sys
print(json.dumps(sys.argv[1:]))
" "${MECH[@]}")
  python3 -c "
import json, sys
violations = json.loads(sys.argv[1])
advisory = json.loads(sys.argv[2])
advice = ['mechanical pre-check -- fix these exactly, they are not opinions'] + advisory
print(json.dumps({'verdict': 'FAIL', 'violations': violations, 'advice': advice}, separators=(',', ':'), ensure_ascii=False))
" "$MECH_JSON" "$ADVISORY_JSON"
  log_gate_verdict "FAIL:mechanical"
  exit 1
fi

PROMPT="You are a strict but CALIBRATED de-slop reviewer.
A deterministic pre-check has ALREADY verified there are no em dashes, no hype vocabulary, and no
filler phrases. Do not look for those; they are handled. Judge only what needs judgment. Below is (1) an anti-slop checklist and (2) an article.

Rules of judgment (calibrated 2026-07-16 after a 12-round non-convergence incident):
- A violation COUNTS ONLY if you can name the specific checklist rule it breaks AND quote the offending text. Format: 'rule: <checklist rule> | quote: <text>'.
- Stylistic preferences that are NOT on the checklist are NOT violations. Do not invent rules.
- Plain declarative factual sentences, numbers, measurements, and first-person reports of real actions (e.g. 'the 30-dollar charge went through') are NOT slop. Never flag them.
- A short sentence that states the article's own MEASURED finding is not a 'quotable' to cut, even if it is punchy. The quotable rule targets pseudo-profound aphorisms the writer did not earn. If a line is backed by a number or an observation elsewhere in the piece, it is earned. Do not flag it.
- SCOPE: judge the ARTICLE BODY only. The free-version footer at the very end (a summary section, then a separator, then two or three plain sentences saying this is the free version and where the full version is, plus a link) is a DELIBERATE, approved structural element -- it is intentionally plain and formulaic. Never flag anything inside it (not as a meta-joiner, not as promotional, not as formulaic).
- If you are unsure whether something violates a rule, it does not.

BLOCKING vs ADVISORY (this is what makes the gate stable — calibrated 2026-07-16 after the judge
flagged different sentences of the SAME text on every run, blocking an article that was already
published and approved):
- Only these are BLOCKING, because they are word-level and any reader can point at them:
  (B1) hype/clickbait vocabulary (shocking, game-changer, revolutionary, must-see, don't miss out, 衝撃/必見/徹底解説...)
  (B2) an em dash (— or --) used as punctuation
  (B3) filler openers/closers (Let's dive in, At the end of the day, いかがでしたか, ぜひチェック...)
  (B4) an unearned aphorism: a punchy general claim with no number or observation anywhere in the piece backing it
  (B5) a factual claim with no source and no first-hand receipt
  (B6) AI self-disclosure / production narration outside the final about-us block -- the article must never present itself as written by an AI, narrate its own production, or talk about 'this article' (the deterministic pre-check already caught the known literal phrasings; this is for every OTHER way of doing the same thing, e.g. narrating what the writer/tool was doing while producing the piece). The final about-us/CTA block (the SAME footer described in SCOPE above) is explicitly exempt -- naming アニッチャ or saying it is an AI THERE is intended and never a B6 hit.
       B6 IS NOT A BAN ON FIRST PERSON. 2026-07-26 correction: B6 was being read as 'diary framing' and was killing human first-person leads, which are among the strongest hooks available (see TITLE PATTERNS 個人の落差型). A human 私/僕/I narrating a stake, a failure, or a measurement the reader can use is CORRECT writing and never a B6 hit. B6 fires only when the narrator is the machine or the piece is about its own making. If you cannot point at either the AI or the production process, it is not B6.
  (B7) spec #72 rule 3, 2026-07-17: a diagram/mermaid figure that carries NO real information --
       a generic self-evident flow (e.g. a bare A→B→C with labels that just restate the
       surrounding prose word-for-word, no real named entities, no real numbers, no real
       relationship the text did not already state as plainly). The mechanical pre-check
       (P3) only verifies a figure EXISTS; THIS checks whether it actually teaches the reader
       something the prose alone did not. A diagram that names real entities from the piece
       (a real protocol/component/step actually discussed) and shows a real relationship
       between them (not just restating adjacency) is NOT a B7 hit even if simple. A table
       with only placeholder-looking values or a single throwaway row is the same defect and
       also counts as B7.
  (B8) spec #77, 2026-07-17, ONLY IF a TITLE section appears below (skip B8 entirely otherwise;
       runs on EVERY platform including zenn/devto, unlike P5's note-only mechanical count):
       list every proper noun or technical term in the TITLE (English or katakana; protocol
       names, language names, product names -- e.g. Virtuals, Solidity, ACP) that would mean
       NOTHING to a reader with zero knowledge of this specific domain. Widely-known general
       words are NOT jargon even if capitalized (AI, Web3, market, contract, crypto) -- judge
       whether an ordinary reader has plausibly heard the word before, not whether it is
       capitalized. B8 fires if EITHER (a) 2 or more such domain-specific terms are listed, OR
       (b) fewer than 2 but a reader with zero domain knowledge genuinely could not tell, from
       the title alone, roughly what the article is about AND why it is a surprising/notable
       finding. Same initialism stoplist as P5 (AI/IT/API/SDK/URL/HTTP/HTTPS/JSON/PDF/CEO/CTO/
       ROI/KPI/ID/UI/UX/OS/PC/QA/FAQ) never counts as jargon. Real miss (the case this rule
       exists to catch): a zenn-staged title read 'Virtualsの求人市場を覗いたら、承認は
       Solidityのハンコ一つで済んでいた' -- 2 jargon terms (Virtuals, Solidity), a B8 hit.
       The fix replaces jargon with its FUNCTION/CATEGORY, not a translation of the same
       proper noun: 'AIが仕事を受発注し合う市場を覗いたら、検収係は中身を見ずに5%もらって
       いた' -- same finding, zero jargon, still specific (a concrete role and a concrete
       number). If the title genuinely needs an irreducible proper noun, that is a body-line-1
       problem, not a title problem: push the name into the first sentence, not the headline.
- EVERYTHING ELSE IS ADVISORY, never blocking. In particular, judgments about rhythm, sentence
  length, fragments, 'formulaic structure', 'dramatic fragmentation', 'rhetorical setup',
  'meta-joiner', or heading style are ADVICE ONLY. Terse deliberate prose ('Deciding, first.
  Paying, second.') is a legitimate authorial choice, not slop. Report it under advice if you
  like; never let it FAIL the piece.

Then output the FINAL LINE as pure JSON exactly:
{\"verdict\":\"PASS\"|\"FAIL\",\"violations\":[\"...\"],\"advice\":[\"...\"]}
where violations = BLOCKING hits only (B1-B8, each 'rule: <B#> | quote: <text>') and advice =
everything else. FAIL only if: any B1 appears in a heading, OR any B6 hit exists, OR any B7
hit exists, OR any B8 hit exists, OR violations has 3 or more entries. Otherwise PASS. A PASS
with a long advice list is normal and correct.

=== CHECKLIST ===
$(cat "$SKILL")
$([ -n "$TITLE_ARG" ] && printf '\n=== TITLE ===\n%s\n' "$TITLE_ARG")
=== ARTICLE ===
$(cat "$MD")"

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
# merge the mechanical vague-quantifier advisory (spec #72 rule 4, computed above) into the
# judge's own "advice" array -- never touches "verdict"/"violations", advisory only.
MERGED_JSON=$(python3 -c "
import json, sys
d = json.loads(sys.argv[1])
extra = json.loads(sys.argv[2])
d['advice'] = d.get('advice', []) + extra
print(json.dumps(d, separators=(',', ':'), ensure_ascii=False))
" "$JSON" "$ADVISORY_JSON" 2>/dev/null || printf '%s' "$JSON")
echo "$MERGED_JSON"
JUDGE_VERDICT=$(printf '%s' "$JSON" | grep -o '"verdict":"[A-Z]*"' | head -1 | cut -d'"' -f4)
log_gate_verdict "${JUDGE_VERDICT:-UNKNOWN}"
printf '%s' "$JSON" | grep -q '"verdict":"PASS"'
