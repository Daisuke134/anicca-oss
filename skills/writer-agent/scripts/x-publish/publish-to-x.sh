#!/bin/bash
# A3 — one-tap X-Article orchestrator (the hands). NEVER /tmp; data in Writer state.
#   publish <md> [--mode draft] [--lang ja|en]  → language-purity + de-slop + eval gates
#                                  (same as run.sh's STEP 6a/6c/6d; seo-gate excluded, it
#                                  needs --title/--meta this path does not have) → prep
#                                  (clean tables + mermaid) → parse → draft on the
#                                  daily-driver (CDP :9222). prints DRAFT_URL. --lang wins
#                                  over content-detection over the (brittle) slug suffix.
#                                  Quality failures can be advisory when ARTICLE_QUALITY_ADVISORY=1.
#                                  (publish/--mode go = guarded manual step, not auto.)
#   verify  <draftUrl>           → measure every image px + screenshot sections (the AGENT Reads them).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$DIR/../writer-runtime-env.sh"
QUALITY_ADVISORY_MODE="${ARTICLE_QUALITY_ADVISORY:-0}"
QUALITY_GATES_ALL_PASS=1
run_quality_gate() {
  local label="$1"
  shift
  local raw_file rc advisory_log attempt
  raw_file="$(mktemp /tmp/article-quality-gate.XXXXXX)" || return 1
  if "$@" >"$raw_file" 2>&1; then
    cat "$raw_file"
    rm -f "$raw_file"
    return 0
  else
    rc=$?
  fi
  cat "$raw_file" >&2
  if [ "$QUALITY_ADVISORY_MODE" = "1" ]; then
    QUALITY_GATES_ALL_PASS=0
    advisory_log="${ARTICLE_QUALITY_ADVISORY_LOG:-${MD%.md}.quality-advisory.jsonl}"
    attempt="${ARTICLE_QUALITY_ATTEMPT:-1}"
    if ! python3 "$DIR/../record-quality-advisory.py" \
      --output "$advisory_log" --gate "$label" --lang "$LANG_X" \
      --attempt "$attempt" --exit-code "$rc" --raw-file "$raw_file"; then
      rm -f "$raw_file"
      return 1
    fi
    rm -f "$raw_file"
    echo "WARN: quality gate $label did not pass; recorded advisory and continuing with best current draft"
    return 0
  fi
  rm -f "$raw_file"
  return 1
}
VC="${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}"
HBPY="$VC"
PARSE="$HOME/.claude/skills/x-article-publisher/scripts/parse_markdown.py"
WORK="$NOTE_WORK_ROOT"; mkdir -p "$WORK"
filt(){ grep -vE "Update available|pip install|fonts" || true; }
cmd="${1:-}"; shift || true
case "$cmd" in
  publish)
    MD="${1:-}"; shift || true
    MODE="draft"; LANG_X=""
    while [ $# -gt 0 ]; do case "$1" in --mode) MODE="$2"; shift 2;; --lang) LANG_X="$2"; shift 2;; *) shift;; esac; done
    [ -f "$MD" ] || { echo "md not found: $MD"; exit 2; }
    # fail-closed PII gate: no operator identifier may reach an X Article. Any non-zero
    # exit (finding / unconfigured blocklist / scanner error) aborts before the draft exists.
    "$HBPY" "$DIR/../pii-gate.py" --stage x-article-stage "$MD" >&2 || exit $?

    SLUG="$(basename "$MD" .md)"
    # title from the source frontmatter → X_TITLE so prep prepends the H1 (else parse_markdown grabs "---" as the title)
    X_TITLE="$(grep -m1 '^title:' "$MD" | sed -E 's/^title:[[:space:]]*//; s/^["'"'"']//; s/["'"'"']$//')"
    # EN articles: do NOT use the JP thumb cover (it has Japanese text) — pass an EN cover via X_COVER, else none
    case "$SLUG" in *en) export X_COVER="${X_COVER:-}";; esac
    # HARD GATE (2026-07-16): parse_markdown treats images[0] as the COVER. Without a prepended
    # cover, the article's first body figure is silently consumed as cover and vanishes from the
    # body (real incident: fig1 lost, figs:3 -> composer:2, zero errors). So a cover is mandatory.
    [ -n "${X_COVER:-}" ] && [ -f "${X_COVER:-}" ] || { echo "FATAL: X_COVER is required (file must exist) — without it the first body figure is silently eaten as cover. export X_COVER=<png>"; exit 4; }
    STAGE_PAIR=""
    case "${ARTICLE_PUBLISH_PAIR:-}" in
      x-article/ja|x-article/en) STAGE_PAIR="$ARTICLE_PUBLISH_PAIR" ;;
      "")
        if [ -n "${ARTICLE_RUN_DIR:-}" ] || [ -n "${ARTICLE_PUBLICATION_STATE:-}" ] || [ -n "${ARTICLE_LEDGER:-}" ]; then
          echo "REFUSED: managed X Article staging requires ARTICLE_PUBLISH_PAIR"; exit 3
        fi ;;
      *) echo "REFUSED: invalid X Article staging pair"; exit 3 ;;
    esac
    if [ -n "$STAGE_PAIR" ]; then
      TARGET_JSON="$("$HBPY" "$DIR/../publication-guard.py" query-target --pair "$STAGE_PAIR")" || exit $?
      if [ "$(printf '%s' "$TARGET_JSON" | jq -r '.found // false')" = "true" ]; then
        SAVED_TARGET="$(printf '%s' "$TARGET_JSON" | jq -r '.target // empty')"
        echo "DRAFT_URL: $SAVED_TARGET"
        echo "STABLE_TARGET_REUSED pair=$STAGE_PAIR"
        exit 0
      fi
    fi

    # HARD GATE (task #16, 2026-07-16): X staging bypassed run.sh entirely (called directly),
    # so it never ran the language-purity / de-slop / eval gates every other channel does --
    # MEASURED same-morning proof: devto/substack-en got blocked by the EN gate while x-en
    # staged clean past it on the identical article. seo-gate is deliberately excluded: it
    # needs --title/--meta this script's callers do not supply, and it is a note/zenn/devto
    # requirement (SEO), not a universal content-quality one.
    if [ -z "$LANG_X" ]; then
      # --lang wins if given. Otherwise detect from the md's own content (same CJK range
      # language-purity-gate.sh itself uses) -- NOT the slug's -en suffix, which is brittle
      # (an EN article need not be named *-en). Slug suffix is only the last-resort fallback.
      CJK_COUNT="$(python3 -c "
import re, sys
CJK_RE = re.compile(r'[぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾝ]')
print(len(CJK_RE.findall(open(sys.argv[1], encoding='utf-8').read())))
" "$MD" 2>/dev/null || echo "")"
      if [ -n "$CJK_COUNT" ]; then
        if [ "$CJK_COUNT" -gt 20 ]; then LANG_X="ja"; else LANG_X="en"; fi
      else
        case "$SLUG" in *-en) LANG_X="en" ;; *) LANG_X="ja" ;; esac
      fi
    fi
    echo "[gate] lang=$LANG_X"

    QUALITY_PHASE_TERMINAL=0
    if [ -n "${ARTICLE_RUN_DIR:-}" ] && python3 "$DIR/../quality-phase-terminal.py" check \
      --run-dir "$ARTICLE_RUN_DIR" --lang "$LANG_X" --markdown-file "$MD"; then
      QUALITY_PHASE_TERMINAL=1
      echo "[gate] terminal run artifact found; skipping channel-level quality re-execution (lang=$LANG_X)"
    fi

    if [ "$QUALITY_PHASE_TERMINAL" -eq 0 ]; then
    if ! run_quality_gate "language-purity" bash "$DIR/../language-purity-gate.sh" --markdown-file "$MD" --lang "$LANG_X"; then
      echo "FATAL: language-purity gate FAILED (lang=$LANG_X) — rewrite to pure $LANG_X before retry"; exit 6
    fi
    # de-slop + eval spawn a fresh model judge each -- cache the verdict per md content hash,
    # SHARED with run.sh's own GATES_STAMP file/convention (${MD%.md}.gates-ok) so the same
    # article is not re-judged once per channel.
    GATES_STAMP="${MD%.md}.gates-ok"
    MD_HASH="$(md5 -q "$MD" 2>/dev/null || md5sum "$MD" | cut -d' ' -f1)"
    if [ ! -f "$GATES_STAMP" ] || [ "$(cat "$GATES_STAMP")" != "$MD_HASH" ]; then
      # spec #57 (superseded for ja, same dual-checklist decision as run.sh, 2026-07-17):
      # ja now runs BOTH stop-ai-slop-jp (G1a) and japanese-tech-writing/k16 (G1b) as two
      # independent fresh-judge calls, blocking, lane/doc-type irrelevant. en unchanged.
      if [ "$LANG_X" = "ja" ]; then
        if ! run_quality_gate "deslop-ja-note" bash "$DIR/../deslop-gate.sh" --markdown-file "$MD" --lang ja --doc-type note; then
          echo "FATAL: de-slop gate G1a (stop-ai-slop-jp) FAILED — fix every listed violation, then retry"; exit 6
        fi
        if ! run_quality_gate "deslop-ja-tech" bash "$DIR/../deslop-gate.sh" --markdown-file "$MD" --lang ja --doc-type tech; then
          echo "FATAL: de-slop gate G1b (japanese-tech-writing / k16) FAILED — fix every listed violation, then retry"; exit 6
        fi
      elif ! run_quality_gate "deslop-$LANG_X" bash "$DIR/../deslop-gate.sh" --markdown-file "$MD" --lang "$LANG_X"; then
        echo "FATAL: de-slop gate FAILED — fix every listed violation, then retry"; exit 6
      fi
      if ! run_quality_gate "eval-$LANG_X" bash "$DIR/../eval-gate.sh" --markdown-file "$MD" --lang "$LANG_X" --title "$X_TITLE"; then
        echo "FATAL: eval gate FAILED (score<56/80, unsourced claims, weak claim-to-artifact ratio, or payment_verdict=no) — rewrite, then retry"; exit 6
      fi
      if [ "$QUALITY_GATES_ALL_PASS" = "1" ]; then
        printf '%s' "$MD_HASH" > "$GATES_STAMP"
      else
        echo "[gate] advisory findings recorded; PASS cache not written"
      fi
    else
      echo "[gate] cached PASS for unchanged md ($MD_HASH)"
    fi
    fi

    export X_SRC="$MD" X_DST="$WORK/$SLUG-x.md" X_ASSETS="$WORK/$SLUG-x-assets" X_TITLE
    export WRITER_X_CLIPBOARD_HELPER="$DIR/browser_clipboard.py"
    mkdir -p "$X_ASSETS"
    echo "[1/3] prep (tables→clean HTML renderer, mermaid→kroki)"; "$VC" "$DIR/prep-x-md.py" | filt
    echo "[2/3] parse"; "$HBPY" "$PARSE" "$X_DST" --output json > "$WORK/$SLUG-x-parsed.json"
    export X_PARSED="$WORK/$SLUG-x-parsed.json"
    # HARD GATE 2: producer/consumer count reconciliation. Prepped md holds cover + N body images;
    # parse must yield exactly N content_images (cover consumed). Any other split = an image was
    # silently dropped or misrouted -> refuse to stage a broken draft.
    MD_IMGS=$(grep -c '^!\[' "$X_DST" || true)
    PARSED_IMGS=$(/usr/bin/env jq '.content_images | length' "$X_PARSED")
    if [ "$PARSED_IMGS" -ne $((MD_IMGS - 1)) ]; then
      echo "FATAL: image count mismatch — prepped md has $MD_IMGS images (incl cover) but parse returned $PARSED_IMGS content_images (expected $((MD_IMGS - 1))). A figure would be silently lost."; exit 5
    fi
    echo "[3/3] draft on the daily-driver (title + cover + TYPE-AND-CHUNK body: guarantees image order)"
    X_STAGE_OUT="$("$VC" "$DIR/x_chunked.py" 2>&1)"
    X_STAGE_RC=$?
    printf '%s\n' "$X_STAGE_OUT" | filt
    [ "$X_STAGE_RC" -eq 0 ] || exit "$X_STAGE_RC"
    DRAFT_URL="$(printf '%s\n' "$X_STAGE_OUT" | sed -n 's/^DRAFT_URL:[[:space:]]*//p' | tail -1)"
    [ -n "$DRAFT_URL" ] || { echo "FATAL: X staging returned no stable draft URL"; exit 5; }
    if [ -n "$STAGE_PAIR" ]; then
      "$HBPY" "$DIR/../publication-guard.py" register-intent \
        --pair "$STAGE_PAIR" --target-kind x-draft-url --target "$DRAFT_URL" >/dev/null || exit $?
    fi
    echo "→ AGENT: capture DRAFT_URL above, run 'publish-to-x.sh verify <DRAFT_URL>', Read the screenshots before any publish." ;;
  verify)
    URL="${1:-}"; [ -n "$URL" ] || { echo "usage: verify <draftUrl>"; exit 2; }
    "$VC" "$DIR/x_fullverify.py" "$URL" 650 | filt; rc=${PIPESTATUS[0]}    # 650 = readable-diagram ceiling in X 587px col (was 900, too lenient — 2026-06-28); also flags <110px flat/tiny-text
    echo "→ screenshots: $WORK/fv*.png (AGENT: Read them, judge every table/diagram/size/funnel before any publish)"
    exit $rc ;;
  enable-publish) touch "$WORK/.X_PUBLISH_ENABLED"; echo "X publish ENABLED (sentinel). Now: X_MODE=go publish-to-x.sh go <draftUrl>" ;;
  disable-publish) rm -f "$WORK/.X_PUBLISH_ENABLED"; echo "X publish disabled (sentinel removed)" ;;
  go)
    URL="${1:-}"; [ -n "$URL" ] || { echo "usage: go <draftUrl>"; exit 2; }
    [ -f "$WORK/.X_PUBLISH_ENABLED" ] || { echo "REFUSED: no sentinel. Run 'publish-to-x.sh enable-publish' first (deliberate human act)."; exit 3; }
    [ "${X_MODE:-}" = "go" ] || { echo "REFUSED: set X_MODE=go to publish (second factor)."; exit 3; }
    # fail-closed PII gate before the LIVE commit. In a managed run the frozen artifacts are
    # the exact bytes being published, so re-scan them here even though staging already did.
    if [ -n "${ARTICLE_RUN_DIR:-}" ]; then
      "$HBPY" "$DIR/../pii-gate.py" --stage x-article-go --run-dir "$ARTICLE_RUN_DIR" --pair "${ARTICLE_PUBLISH_PAIR:-}" >&2 || exit $?
    fi

    case "${ARTICLE_PUBLISH_PAIR:-}" in
      x-article/ja|x-article/en) PAIR="$ARTICLE_PUBLISH_PAIR" ;;
      "")
        if [ "${ARTICLE_AUTOPUBLISH:-0}" = "1" ] || [ -n "${ARTICLE_RUN_DIR:-}" ] || [ -n "${ARTICLE_PUBLICATION_STATE:-}" ] || [ -n "${ARTICLE_LEDGER:-}" ]; then
          echo "REFUSED: managed article publish requires ARTICLE_PUBLISH_PAIR=x-article/ja or x-article/en"; exit 3
        fi
        PAIR="" ;;
      *) echo "REFUSED: ARTICLE_PUBLISH_PAIR must be x-article/ja or x-article/en"; exit 3 ;;
    esac
    trap 'rm -f "$WORK/.X_PUBLISH_ENABLED"' EXIT
    if [ -n "$PAIR" ]; then
      GUARD_OUT=$("$HBPY" "$DIR/../publication-guard.py" preflight --pair "$PAIR" --target-kind x-draft-url --target "$URL") || exit $?
    else
      GUARD_OUT=$("$HBPY" "$DIR/../publication-guard.py" manual-check --pair x-article/ja) || exit $?
    fi
    if [ "$(printf '%s' "$GUARD_OUT" | jq -r '.action // empty')" = "skip-live" ]; then
      echo "ALREADY_LIVE_SKIP url=$(printf '%s' "$GUARD_OUT" | jq -r '.live_url')"
      exit 0
    fi
    "$VC" "$DIR/x-go.py" "$URL" | filt; rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ] && [ -n "$PAIR" ]; then
      # Re-open the same stable draft URL.  X redirects a published draft to its live view;
      # publication_remote records that URL and repairs a missing ledger row atomically.
      "$HBPY" "$DIR/../publication-guard.py" reconcile --pair "$PAIR" || rc=$?
    fi
    echo "→ AGENT: verify the LIVE Article in the browser (size/honesty/no-funnel) per HARD 0.31."
    exit $rc ;;
  *) echo "usage: publish-to-x.sh publish <md> [--mode draft] | verify <draftUrl> | enable-publish | go <draftUrl> | disable-publish"; exit 2 ;;
esac
