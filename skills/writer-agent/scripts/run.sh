#!/usr/bin/env bash
# Writer Agent orchestrator for daily article publication
# Phases:
#   --phase propose → context bundle JSON to stdout
#   --phase publish → SEO gate + publisher dispatch + history record + verify
#
# Usage:
#   bash run.sh --channel <zenn|devto|substack-ja|substack-en|aniccaai-blog> --phase propose
#   bash run.sh --channel <...> --phase publish --markdown-file <f> --title <t> --meta <m> [--slug <s>] [--subtitle <s>]

set -euo pipefail

CHANNEL=""
PHASE=""
MD_FILE=""
TITLE=""
META=""
SLUG=""
SUBTITLE=""
BRIEF_SLUG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel)        CHANNEL="$2"; shift 2 ;;
    --phase)          PHASE="$2"; shift 2 ;;
    --markdown-file)  MD_FILE="$2"; shift 2 ;;
    --title)          TITLE="$2"; shift 2 ;;
    --meta)           META="$2"; shift 2 ;;
    --slug)           SLUG="$2"; shift 2 ;;
    --subtitle)       SUBTITLE="$2"; shift 2 ;;
    --brief-slug)     BRIEF_SLUG="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$CHANNEL" && -n "$PHASE" ]] || { echo "FATAL: --channel --phase required" >&2; exit 1; }

# The daily wrapper injects the immutable release root through ARTICLE_SKILL_DIR.
# Falling back to the historical home path made the orchestrator run quality and
# publisher helpers from an empty compatibility directory, so every managed
# destination failed before it could create a draft.  Keep the old default only
# for standalone/manual invocations.
SKILL_DIR="${ARTICLE_SKILL_DIR:-${ARTICLE_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}}"
if [[ ! -d "$SKILL_DIR/scripts" ]]; then
  echo "FATAL: writer skill scripts directory is missing: $SKILL_DIR/scripts" >&2
  exit 1
fi
# shellcheck source=writer-runtime-env.sh
source "$SKILL_DIR/scripts/writer-runtime-env.sh"
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
  if [[ "$QUALITY_ADVISORY_MODE" == "1" ]]; then
    QUALITY_GATES_ALL_PASS=0
    advisory_log="${ARTICLE_QUALITY_ADVISORY_LOG:-${MD_FILE%.md}.quality-advisory.jsonl}"
    attempt="${ARTICLE_QUALITY_ATTEMPT:-1}"
    if ! python3 "$SKILL_DIR/scripts/record-quality-advisory.py" \
      --output "$advisory_log" --gate "$label" --lang "$LANG" \
      --attempt "$attempt" --exit-code "$rc" --raw-file "$raw_file"; then
      rm -f "$raw_file"
      return 1
    fi
    rm -f "$raw_file"
    echo "⚠ quality gate $label did not pass; recorded advisory and continuing with best current draft" >&2
    return 0
  fi
  rm -f "$raw_file"
  return 1
}

# Publication identities are installation settings. They come from the private
# Life Manager environment and never from an operator-specific source default.
case "$CHANNEL" in
  zenn)           LANG="ja"; PLATFORM="Zenn";        ACCOUNT="${ZENN_ACCOUNT:?ZENN_ACCOUNT is required}" ;;
  devto)          LANG="en"; PLATFORM="Dev.to";      ACCOUNT="${DEVTO_ACCOUNT_HANDLE:?DEVTO_ACCOUNT_HANDLE is required}" ;;
  substack-ja)    LANG="ja"; PLATFORM="Substack";    ACCOUNT="${SUBSTACK_PUBLICATION_JA:-${SUBSTACK_PUBLICATION:?SUBSTACK_PUBLICATION_JA or SUBSTACK_PUBLICATION is required}}" ;;
  substack-en)    LANG="en"; PLATFORM="Substack";    ACCOUNT="${SUBSTACK_PUBLICATION_EN:?SUBSTACK_PUBLICATION_EN is required for substack-en}" ;;
  note)           LANG="ja"; PLATFORM="Note";        ACCOUNT="${NOTE_URLNAME:?NOTE_URLNAME is required}" ;;
  aniccaai-blog)  LANG="ja"; PLATFORM="aniccaai-blog"; ACCOUNT="${ARTICLE_BLOG_ACCOUNT:?ARTICLE_BLOG_ACCOUNT is required}" ;;
  *) echo "FATAL: unknown channel: $CHANNEL" >&2; exit 1 ;;
esac

case "$PHASE" in
  propose)
    exec bash "$SKILL_DIR/scripts/propose.sh" --channel "$CHANNEL"
    ;;

  publish)
    [[ -f "$MD_FILE" && -n "$TITLE" && -n "$META" ]] || { echo "FATAL: publish requires --markdown-file --title --meta" >&2; exit 1; }

    QUALITY_PHASE_TERMINAL=0
    if [[ -n "${ARTICLE_RUN_DIR:-}" ]] && python3 "$SKILL_DIR/scripts/quality-phase-terminal.py" check \
      --run-dir "$ARTICLE_RUN_DIR" --lang "$LANG" --markdown-file "$MD_FILE"; then
      QUALITY_PHASE_TERMINAL=1
      echo "gates: terminal run artifact found; skipping channel-level quality re-execution (lang=$LANG)" >&2
    fi

    if [[ "$QUALITY_PHASE_TERMINAL" -eq 0 ]]; then
    # STEP 6a: language-purity gate (Dais 2026-06-03 厳命 — ja/en 混在禁止)
    if ! run_quality_gate "language-purity" bash "$SKILL_DIR/scripts/language-purity-gate.sh" --markdown-file "$MD_FILE" --lang "$LANG"; then
      echo "❌ language-purity gate FAILED (lang=$LANG) — rewrite to pure $LANG before retry" >&2
      exit 1
    fi

    # STEP 6b: SEO gate (mandatory)
    if ! run_quality_gate "seo" bash "$SKILL_DIR/scripts/seo-gate.sh" --title "$TITLE" --meta "$META" --markdown-file "$MD_FILE" --lang "$LANG"; then
      echo "❌ SEO gate FAILED — fix article and retry" >&2
      exit 1
    fi

    # STEP 6c+6d: de-slop gate (#17) + eval gate (#18). Both spawn a fresh model judge, so
    # cache the verdict per md content hash — publish is invoked once per channel and the
    # same article must not be re-judged five times. Any edit to the md invalidates the cache.
    GATES_STAMP="${MD_FILE%.md}.gates-ok"
    MD_HASH=$(md5 -q "$MD_FILE" 2>/dev/null || md5sum "$MD_FILE" | cut -d' ' -f1)
    if [[ ! -f "$GATES_STAMP" ]] || [[ "$(cat "$GATES_STAMP")" != "$MD_HASH" ]]; then
      # spec #57 (superseded for ja by the 2026-07-17 dual-checklist decision below):
      # --doc-type used to let the caller pick ONE of note-voice (stop-ai-slop-jp) or
      # technical-writing (k16/japanese-tech-writing) for ja. Dais decided both must run --
      # concatenating them into one prompt was already rejected (non-convergence risk), so
      # this is two independent fresh-judge calls (G1a note, G1b tech), lane/doc-type
      # irrelevant, both blocking. en is unaffected (stop-slop only, unchanged).
      if [[ "$LANG" == "ja" ]]; then
        # --title/--platform (spec #72 rule 5): CHANNEL is already the exact lowercase value
        # deslop-gate.sh's P5 note-title check expects ("note"); every other channel value is
        # passed through too but P5 only ever fires when it is literally "note".
        if ! run_quality_gate "deslop-ja-note" bash "$SKILL_DIR/scripts/deslop-gate.sh" --markdown-file "$MD_FILE" --lang ja --doc-type note --title "$TITLE" --platform "$CHANNEL"; then
          echo "❌ de-slop gate G1a (stop-ai-slop-jp) FAILED — fix every listed violation, then retry" >&2
          exit 1
        fi
        if ! run_quality_gate "deslop-ja-tech" bash "$SKILL_DIR/scripts/deslop-gate.sh" --markdown-file "$MD_FILE" --lang ja --doc-type tech --title "$TITLE" --platform "$CHANNEL"; then
          echo "❌ de-slop gate G1b (japanese-tech-writing / k16) FAILED — fix every listed violation, then retry" >&2
          exit 1
        fi
      else
        if ! run_quality_gate "deslop-$LANG" bash "$SKILL_DIR/scripts/deslop-gate.sh" --markdown-file "$MD_FILE" --lang "$LANG"; then
          echo "❌ de-slop gate FAILED — fix every listed violation, then retry" >&2
          exit 1
        fi
      fi
      if ! run_quality_gate "eval-$LANG" bash "$SKILL_DIR/scripts/eval-gate.sh" --markdown-file "$MD_FILE" --lang "$LANG" --title "$TITLE"; then
        echo "❌ eval gate FAILED (score<56/80, unsourced claims, weak claim-to-artifact ratio, or payment_verdict=no) — rewrite, then retry" >&2
        exit 1
      fi
      if [[ "$QUALITY_GATES_ALL_PASS" == "1" ]]; then
        printf '%s' "$MD_HASH" > "$GATES_STAMP"
      else
        echo "gates: advisory findings recorded; PASS cache not written" >&2
      fi
    else
      echo "gates: cached PASS for unchanged md ($MD_HASH)" >&2
    fi
    fi

    # Publisher dispatch
    case "$CHANNEL" in
      zenn)
        URL="$(bash "$SKILL_DIR/scripts/publish-zenn.sh" --markdown-file "$MD_FILE" --title "$TITLE" --meta "$META")" ;;
      devto)
        URL="$(bash "$SKILL_DIR/scripts/publish-devto.sh" --markdown-file "$MD_FILE" --title "$TITLE" --meta "$META")" ;;
      substack-ja|substack-en)
        export SUBSTACK_PUBLICATION="$ACCOUNT"
        # publish-substack.sh alone ships raw ```mermaid fences unrendered (Substack does not
        # render mermaid) -- publish-substack-mermaid.sh wraps it with the kroki->PNG->upload
        # step first (spec #45), draft-only by default, same as before.
        URL="$(bash "$SKILL_DIR/scripts/_shared/publish-substack-mermaid.sh" publish "$MD_FILE" --title "$TITLE" --subtitle "${SUBTITLE:-$META}" | tail -1)" ;;
      note)
        URL="$(bash "$SKILL_DIR/scripts/publish-note.sh" --markdown-file "$MD_FILE" --title "$TITLE" --description "$META")" ;;
      aniccaai-blog)
        URL="$(bash "$SKILL_DIR/scripts/publish-aniccaai-blog.sh" --markdown-file "$MD_FILE" --title "$TITLE" --meta "$META" ${SLUG:+--slug "$SLUG"})" ;;
    esac

    if [[ -z "${URL:-}" ]]; then
      echo "❌ publisher returned empty URL" >&2
      exit 1
    fi

    # note channel only: publish-note.sh's stdout contract is a single opaque line (never a
    # real URL, draft-only by design — see publish-note.sh's own comment on this), and it now
    # carries stage1_ok=/stage2_ok= tokens on that same line so a stage1-render or stage2-embed
    # failure that silently degraded the draft (raw markdown / raw @@TBL@@ markers instead of
    # images) is visible below in meta.json + account-history instead of only in stderr.
    # `if VAR=$(cmd); then :; else VAR=""; fi` (not a bare `VAR=$(cmd)`) because under
    # `set -o pipefail` a grep-no-match makes the pipeline exit non-zero, which would kill this
    # script under `set -e` for every non-note channel where these tokens are simply absent.
    NOTE_STAGE1_OK=""; NOTE_STAGE2_OK=""; NOTE_STAGE2_EMBEDDED=""; NOTE_REUSED=""
    if [[ "$CHANNEL" == "note" ]]; then
      if NOTE_STAGE1_OK=$(printf '%s' "$URL" | grep -oE 'stage1_ok=[a-z]+' | head -1 | cut -d= -f2); then :; else NOTE_STAGE1_OK=""; fi
      if NOTE_STAGE2_OK=$(printf '%s' "$URL" | grep -oE 'stage2_ok=[a-z]+' | head -1 | cut -d= -f2); then :; else NOTE_STAGE2_OK=""; fi
      # "N/M images actually embedded" — see note-stage2-publish.py's EMBED_SUMMARY / publish-note.sh's
      # stage2_embedded= token. Empty when stage2 didn't run at all (stage1 unavailable / no DRAFT_NUM).
      if NOTE_STAGE2_EMBEDDED=$(printf '%s' "$URL" | grep -oE 'stage2_embedded=[0-9]+/[0-9]+' | head -1 | cut -d= -f2); then :; else NOTE_STAGE2_EMBEDDED=""; fi
      # reused=true means publish-note.sh's local idempotency ledger hit -> this run UPDATED an
      # existing draft instead of creating a new one (see publish-note.sh's note-draft-ledger.py wiring).
      if NOTE_REUSED=$(printf '%s' "$URL" | grep -oE 'reused=[a-z]+' | head -1 | cut -d= -f2); then :; else NOTE_REUSED=""; fi
    fi

    # STEP 7 verify (Dais 2026-07-12, content-check fix same day): this loop is DRAFT-ONLY.
    # A draft id / draft URL / "DRAFT (unpublished) ..." string is the expected publisher
    # return value now, and a public URL that is not live to an anonymous request is the
    # CORRECT outcome for Zenn/dev.to -- both are SUCCESS. A raw HTTP-status check is not
    # enough: Zenn serves an unpublished-but-existing article slug as a client-rendered
    # "ページが見つかりません" (not found) shell with HTTP 200, not a real 404 -- a bare
    # status-code check flags that as "publicly live" (false positive, verified against a
    # genuinely published Zenn article, which instead 301-redirects to the canonical
    # username then returns a real <title> + real body). So for zenn/devto, fetch the body
    # and look for actual article content, not just the status code.
    #
    # ARTICLE_AUTOPUBLISH gate (U5): this staging call (--phase publish) never itself goes
    # live -- the guarded live-publish path is a wholly separate script per platform
    # (publish-paid.py, publish-to-zenn.sh, publish-substack-mermaid.sh, publish-to-x.sh),
    # invoked later by article-daily.sh's STEP 13/15/16/17, never from here. So a public-live
    # result here is unconditionally a SAFETY FAILURE for devto/aniccaai-blog (never in scope
    # for going live, armed or not) and, by default (ARTICLE_AUTOPUBLISH unset/0), for every
    # channel -- byte-identical to the draft-only behavior before this change. Only when
    # ARTICLE_AUTOPUBLISH=1 is armed AND the channel is one that addendum actually takes live
    # (note/zenn/substack-ja/substack-en) does a public-live result stop being an automatic
    # hard failure here: a same-day incomplete-pass respawn can legitimately re-invoke this
    # staging call on a slug/URL a PRIOR attempt already took live via the guarded path earlier
    # in the same pass, and that is the expected outcome, not a bug. This never gives this
    # script a new way to publish -- it only stops it from crying wolf about a live state some
    # other, already-guarded script legitimately created.
    AUTOPUBLISH="${ARTICLE_AUTOPUBLISH:-0}"
    LIVE_ELIGIBLE=0
    case "$CHANNEL" in
      note|zenn|substack-ja|substack-en) LIVE_ELIGIBLE=1 ;;
    esac
    case "$CHANNEL" in
      zenn)
        BODY="$(curl -sL --max-time 15 "$URL" 2>/dev/null || echo "")"
        if printf '%s' "$BODY" | grep -q "見つかりません"; then
          echo "STEP 7 verify OK: zenn shows not-found shell to anonymous request — draft, not live" >&2
        elif printf '%s' "$BODY" | grep -qE '<title[^>]+>[^<]+</title>'; then
          if [[ "$AUTOPUBLISH" == "1" && "$LIVE_ELIGIBLE" == "1" ]]; then
            echo "STEP 7 verify: zenn article is PUBLICLY LIVE (real <title>+body) -- expected under ARTICLE_AUTOPUBLISH=1 only via the guarded STEP 16 publish-to-zenn.sh path, never from this staging call itself. URL=$URL" >&2
          else
            echo "❌ SAFETY FAILURE: zenn article appears to be PUBLICLY LIVE (real <title>+body served to anonymous request) but this loop must not be public — it is draft-only. URL=$URL" >&2
            exit 1
          fi
        else
          echo "STEP 7 verify OK: zenn body has no real article title — treating as not live" >&2
        fi
        ;;
      *)
        HTTP="$(curl -sI -o /dev/null -w "%{http_code}" "$URL" 2>/dev/null || echo 000)"
        if [[ "$HTTP" == "200" ]]; then
          if [[ "$AUTOPUBLISH" == "1" && "$LIVE_ELIGIBLE" == "1" ]]; then
            echo "STEP 7 verify: $CHANNEL article is PUBLICLY LIVE (HTTP 200) -- expected under ARTICLE_AUTOPUBLISH=1 only via the guarded live-publish path (STEP 13/17), never from this staging call itself. URL=$URL" >&2
          else
            echo "❌ SAFETY FAILURE: $CHANNEL article appears to be PUBLICLY LIVE (HTTP 200) but this loop must not be public — it is draft-only. URL=$URL" >&2
            exit 1
          fi
        else
          echo "STEP 7 verify OK: $CHANNEL not publicly live (HTTP=$HTTP) — draft/expected-404 outcome" >&2
        fi
        ;;
    esac

    # Record to account-history (HR-C) — honest draft status, not a fabricated "posted"
    . "$LIFE_MANAGER_REPO/skills/_shared/lib/account-history.sh"
    CTX="$(bash "$SKILL_DIR/scripts/propose.sh" --channel "$CHANNEL" 2>/dev/null || echo '{}')"
    PATTERN_ID="$(printf '%s' "$CTX" | jq -r '.pattern.source_id // "unknown"')"
    STRUCT_TYPE="$(printf '%s' "$CTX" | jq -r '.pattern.niche_tags[0] // "article"')"
    SNIPPET="${META:0:240}"
    [[ -n "$NOTE_STAGE1_OK" ]] && SNIPPET="${SNIPPET} [stage1_ok=${NOTE_STAGE1_OK} stage2_ok=${NOTE_STAGE2_OK} stage2_embedded=${NOTE_STAGE2_EMBEDDED:-n/a} reused=${NOTE_REUSED:-n/a}]"
    ah_record "article-${CHANNEL}" "$PLATFORM" "$ACCOUNT" "$TITLE" "$STRUCT_TYPE" "$PATTERN_ID" "$SNIPPET" "draft"

    # Archive meta in the external Life Manager state root.  The release tree is
    # immutable; falling back to `$SKILL_DIR/state` is only valid for standalone
    # manual invocations that did not provide ARTICLE_STATE_DIR.
    STATE_DIR="${ARTICLE_STATE_DIR:-$SKILL_DIR/state}"
    mkdir -p "$STATE_DIR"
    META_FILE="$STATE_DIR/${CHANNEL}-$(date +%Y%m%d-%H%M).meta.json"
    jq -nc \
      --arg url "$URL" \
      --arg pat "$PATTERN_ID" \
      --arg channel "$CHANNEL" \
      --arg title "$TITLE" \
      --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      --arg bslug "$BRIEF_SLUG" \
      --arg s1 "$NOTE_STAGE1_OK" \
      --arg s2 "$NOTE_STAGE2_OK" \
      --arg s2emb "$NOTE_STAGE2_EMBEDDED" \
      --arg reused "$NOTE_REUSED" \
      '{release_url:$url, pattern_id:$pat, channel:$channel, title:$title, posted_at:$ts, brief_slug:$bslug}
       + (if $s1 == "" then {} else {stage1_ok: ($s1 == "true")} end)
       + (if $s2 == "" then {} else {stage2_ok: ($s2 == "true")} end)
       + (if $s2emb == "" then {} else {stage2_embedded: $s2emb} end)
       + (if $reused == "" then {} else {draft_reused: ($reused == "true")} end)' \
      > "$META_FILE"

    echo "✅ $CHANNEL drafted (not published): $URL pattern=$PATTERN_ID title=$(printf '%s' "$TITLE" | cut -c1-50)"
    exit 0
    ;;

  *) echo "FATAL: unknown phase: $PHASE (use propose|publish)" >&2; exit 1 ;;
esac
