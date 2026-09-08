#!/usr/bin/env bash
# publish-substack-mermaid.sh — "today's manual procedure" (spec #45), fixed into one command:
#   mermaid fences -> kroki PNG -> Substack image upload (S3-backed CDN) -> md figure swap
#   -> publish-substack.sh draft -> (gated) live publish with send=false -> self-verify GET.
# Mirrors substack-publish/publish-to-substack.sh's own deliberate-human gate for the live
# step: publishing is opt-in per run (sentinel file + SUBSTACK_MODE=go), never automatic —
# scripts/publish-substack.sh itself stays permanently draft-only (Dais 2026-07-12), this
# wrapper adds an EXPLICIT, separately-gated publish step on top of it rather than changing
# that contract.
#
# Usage:
#   publish-substack-mermaid.sh publish <md> --title "T" [--subtitle "S"] [--mode draft|go]
#   publish-substack-mermaid.sh enable-publish | disable-publish
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # .../scripts
# shellcheck source=../writer-runtime-env.sh
source "$DIR/writer-runtime-env.sh"
. "$DIR/substack-publish/substack-curl.sh"
VC="${VC:-$WRITER_BROWSER_PYTHON}"   # only for the verify-preview vision gate
WORK="$HOME/.cloak/note-work"; mkdir -p "$WORK"

SUBSTACK_PUBLICATION="${SUBSTACK_PUBLICATION:?SUBSTACK_PUBLICATION is required}"

cmd="${1:-}"; shift || true
case "$cmd" in
  publish)
    MD=""; TITLE=""; SUBTITLE=""; MODE="draft"
    if [ $# -gt 0 ] && [[ "$1" != --* ]]; then MD="$1"; shift; fi
    while [ $# -gt 0 ]; do case "$1" in
      --markdown-file) MD="$2"; shift 2 ;;
      --title)         TITLE="$2"; shift 2 ;;
      --subtitle)       SUBTITLE="$2"; shift 2 ;;
      --mode)          MODE="$2"; shift 2 ;;
      *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac; done
    [ -f "$MD" ] && [ -n "$TITLE" ] || { echo "FATAL: --markdown-file and --title required" >&2; exit 1; }
    # fail-closed PII gate: covers BOTH --mode draft and --mode go, so neither the draft nor
    # the live flip can carry an operator identifier. Any non-zero exit aborts the publish.
    python3 "$DIR/pii-gate.py" --stage publish-substack-wrapper "$MD" >&2 || exit $?

    PAIR=""
    case "${ARTICLE_PUBLISH_PAIR:-}" in substack/ja|substack/en) PAIR="$ARTICLE_PUBLISH_PAIR" ;; esac
    MANAGED=0
    if [ "${ARTICLE_AUTOPUBLISH:-0}" = "1" ] || [ -n "${ARTICLE_RUN_DIR:-}" ] || [ -n "${ARTICLE_PUBLICATION_STATE:-}" ] || [ -n "${ARTICLE_LEDGER:-}" ] || [ -n "${ARTICLE_PUBLISH_PAIR:-}" ]; then MANAGED=1; fi
    if [ "$MANAGED" -eq 1 ] && [ -z "$PAIR" ]; then
      echo "REFUSED: ARTICLE_PUBLISH_PAIR must be substack/ja or substack/en" >&2
      exit 4
    fi
    if [ "$MANAGED" -eq 1 ]; then
      LANG="${PAIR##*/}"
      LANG_UPPER="$(printf '%s' "$LANG" | tr '[:lower:]' '[:upper:]')"
      COOKIE_KEY="SUBSTACK_SESSION_COOKIE_${LANG_UPPER}"
      PAIR_COOKIE="$(printenv "$COOKIE_KEY" 2>/dev/null || true)"
      if [ -z "$PAIR_COOKIE" ] && [ "$LANG" = "ja" ]; then
        PAIR_COOKIE="${SUBSTACK_SESSION_COOKIE:-}"
      fi
      [ -n "$PAIR_COOKIE" ] || {
        echo "REFUSED: $COOKIE_KEY is required for managed Substack publication" >&2
        exit 4
      }
      export SUBSTACK_SESSION_COOKIE="$PAIR_COOKIE"
    fi
    if [ "$MANAGED" -eq 1 ]; then
      LANG="${PAIR##*/}"
      LANG_UPPER="$(printf '%s' "$LANG" | tr '[:lower:]' '[:upper:]')"
      IDENTITY_KEY="SUBSTACK_PUBLICATION_${LANG_UPPER}"
      PAIR_PUBLICATION="$(printenv "$IDENTITY_KEY" 2>/dev/null || true)"
      [ -n "$PAIR_PUBLICATION" ] || {
        echo "REFUSED: $IDENTITY_KEY is required for managed Substack publication" >&2
        exit 4
      }
      PAIR_PUBLICATION="$(printf '%s' "$PAIR_PUBLICATION" | tr '[:upper:]' '[:lower:]')"
      case "$PAIR_PUBLICATION" in *.substack.com) ;; *)
        echo "REFUSED: $IDENTITY_KEY must be a Substack host" >&2
        exit 4
      esac
      EXPECTED_PUBLICATION="$(jq -r --arg pair "$PAIR" '.destination_identities[$pair] // ""' "$ARTICLE_PUBLICATION_STATE" 2>/dev/null || true)"
      EXPECTED_PUBLICATION="$(printf '%s' "$EXPECTED_PUBLICATION" | tr '[:upper:]' '[:lower:]')"
      [ -n "$EXPECTED_PUBLICATION" ] && [ "$EXPECTED_PUBLICATION" = "$PAIR_PUBLICATION" ] || {
        echo "REFUSED: managed Substack identity does not match persisted state for $PAIR" >&2
        exit 4
      }
      SUBSTACK_PUBLICATION="$PAIR_PUBLICATION"
    fi

    SLUG_BASE="$(basename "$MD" .md)"
    EMBEDDED="$WORK/${SLUG_BASE}-substack-embedded.md"

    DRAFT_ID=""
    if [ "$MANAGED" -eq 1 ]; then
      TARGET_JSON="$(python3 "$DIR/publication-guard.py" query-target --pair "$PAIR")" || exit $?
      if [ "$(printf '%s' "$TARGET_JSON" | jq -r '.found // false')" = "true" ]; then
        DRAFT_ID="$(printf '%s' "$TARGET_JSON" | jq -r '.target // empty')"
        echo "== step 1/3: reuse stable Substack draft id=$DRAFT_ID ==" >&2
        echo "DRAFT (reused) id=$DRAFT_ID publication=$SUBSTACK_PUBLICATION"
      fi
    fi

    if [ -z "$DRAFT_ID" ]; then
      if [ "$MODE" = "go" ] && [ "$MANAGED" -eq 1 ]; then
        echo "REFUSED: managed Substack live publish requires a pre-registered draft ID" >&2
        exit 4
      fi
      echo "== step 1/3: mermaid -> Substack images ==" >&2
      EMBED_ARGS=(--markdown-file "$MD" --out "$EMBEDDED")
      if [ "$MANAGED" -eq 1 ]; then
        [ -f "$ARTICLE_RUN_DIR/headline-image.png" ] || {
          echo "FATAL: managed Substack staging requires immutable headline-image.png" >&2
          exit 1
        }
        EMBED_ARGS+=(--headline-image "$ARTICLE_RUN_DIR/headline-image.png")
        while IFS= read -r BODY_IMAGE; do
          [ -f "$BODY_IMAGE" ] || {
            echo "FATAL: managed Substack body asset is missing: $BODY_IMAGE" >&2
            exit 1
          }
          EMBED_ARGS+=(--body-image "$BODY_IMAGE")
        done < <(jq -r '.media.body_assets[].path' "$ARTICLE_PUBLICATION_STATE")
      fi
      python3 "$DIR/_shared/embed-mermaid-substack.py" "${EMBED_ARGS[@]}" 1>&2 || exit 1

      echo "== step 2/3: create draft ==" >&2
      DRAFT_LINE="$(bash "$DIR/publish-substack.sh" --markdown-file "$EMBEDDED" --title "$TITLE" --subtitle "$SUBTITLE")" || exit 1
      echo "$DRAFT_LINE"
      DRAFT_ID="$(printf '%s' "$DRAFT_LINE" | grep -oE 'id=[0-9]+' | head -1 | cut -d= -f2)"
      [ -n "$DRAFT_ID" ] || { echo "FATAL: could not parse draft id from: $DRAFT_LINE" >&2; exit 3; }
    fi

    GUARD_OUT=""
    if [ "$MODE" = "draft" ] && [ "$MANAGED" -eq 1 ]; then
      python3 "$DIR/publication-guard.py" register-intent --pair "$PAIR" \
        --target-kind substack-draft-id --target "$DRAFT_ID" >/dev/null || exit $?
    elif [ "$MODE" = "go" ] && [ -n "$PAIR" ]; then
      GUARD_OUT="$(python3 "$DIR/publication-guard.py" preflight --pair "$PAIR" --target-kind substack-draft-id --target "$DRAFT_ID")" || exit $?
    elif [ "$MODE" = "go" ]; then
      GUARD_OUT="$(python3 "$DIR/publication-guard.py" manual-check --pair substack/ja)" || exit $?
    fi

    if [ "$MODE" = "go" ]; then
      echo "== step 3/3: verify + publish (send=false) + self-verify ==" >&2
      [ -f "$WORK/.SUBSTACK_PUBLISH_ENABLED" ] || { echo "REFUSED: no sentinel. Run 'publish-substack-mermaid.sh enable-publish' first (deliberate human act)." >&2; exit 4; }
      [ "${SUBSTACK_MODE:-}" = "go" ] || { echo "REFUSED: set SUBSTACK_MODE=go to publish (second factor)." >&2; exit 4; }
      # the sentinel is single-use for this attempt regardless of outcome (same as
      # publish-to-substack.sh) -- a fresh 'enable-publish' is required for the next try,
      # success or failure, so a stale sentinel can never silently satisfy the next run.
      trap 'rm -f "$WORK/.SUBSTACK_PUBLISH_ENABLED"' EXIT
      if [ "$(printf '%s' "$GUARD_OUT" | jq -r '.action // empty')" = "skip-live" ]; then
        echo "ALREADY_LIVE_SKIP url=$(printf '%s' "$GUARD_OUT" | jq -r '.live_url')"
        exit 0
      fi

      # The create/repair request is not evidence that Substack retained the
      # subscriber contract. Refuse the live side effect unless authenticated
      # readback shows only_paid + free preview + exactly one paywall node.
      PAID_DRAFT_READBACK="$(substack_curl "$SUBSTACK_PUBLICATION" -sS --fail-with-body \
        "https://${SUBSTACK_PUBLICATION}/api/v1/drafts/${DRAFT_ID}" \
        -H "Cookie: ${SUBSTACK_SESSION_COOKIE}" \
        -H "User-Agent: Mozilla/5.0" \
        -H "Accept: application/json")" || {
          echo "FATAL: authenticated Substack paid-draft readback failed" >&2
          exit 5
        }
      printf '%s' "$PAID_DRAFT_READBACK" | python3 \
        "$DIR/substack-publish/substack_paid_payload.py" \
        --verify-response >/dev/null || {
          echo "FATAL: Substack draft lost its paid subscriber contract" >&2
          exit 5
        }

      # pre-publish VISION gate, same as substack-publish.py's own SUBSTACK_GO path: open the
      # REAL preview, measure every rendered image, refuse if any exceeds the on-screen height
      # budget (kroki's flowchart PNGs are natively portrait — SKILL.md rule 71 — and Substack
      # stretches every body image to the 728px column, so a raw kroki PNG can blow up to fill
      # the page). Best-effort: if the browser session itself is unavailable this still refuses
      # (fail-closed), but the failure may not be about image size specifically -- say so.
      if [ -x "$VC" ]; then
        "$VC" "$DIR/substack-publish/verify-preview.py" "$DRAFT_ID" 950
        if [ $? -ne 0 ]; then
          echo "FATAL: verify-preview FAILED — NOT publishing (an image may be too tall on screen, or the preview could not be opened at all -- check the message above for which)." >&2
          exit 5
        fi
      else
        echo "WARN: verify-preview venv not found at $VC, skipping the pre-publish vision gate (image-size risk not checked)." >&2
      fi

      RESP="$(substack_curl "$SUBSTACK_PUBLICATION" -sS -X POST "https://${SUBSTACK_PUBLICATION}/api/v1/drafts/${DRAFT_ID}/publish" \
        -H "Content-Type: application/json" \
        -H "Cookie: ${SUBSTACK_SESSION_COOKIE}" \
        -H "User-Agent: Mozilla/5.0" \
        -d '{"send":false,"share_automatically":false}')"
      PUB_SLUG="$(printf '%s' "$RESP" | jq -r '.slug // .draft_slug // empty' 2>/dev/null)"
      if [ -z "$PUB_SLUG" ]; then
        echo "FATAL: publish API did not return a slug:" >&2
        printf '%s\n' "$RESP" >&2
        exit 6
      fi
      LIVE_URL="https://${SUBSTACK_PUBLICATION}/p/${PUB_SLUG}"
      echo "PUBLISHED: $LIVE_URL"

      # self-verify (Dais/team-lead requirement): GET the actually-live page and confirm the
      # title and at least one uploaded diagram are really there -- an API 200 is not proof the
      # reader sees a correct page.
      # A just-published post is not readable the instant the API returns: on 2026-07-27 this
      # check reported FATAL on a post that was in fact live, and the identical command passed
      # on retry. One GET after a fixed 2s sleep is a coin flip, so poll instead of failing on
      # the first miss. Matching is done in-shell (no `printf | grep -q`) so the comparison
      # cannot be affected by pipe status at all.
      LIVE_HTML=""
      TITLE_SEEN=0
      for _ATTEMPT in 1 2 3 4 5; do
        sleep 2
        LIVE_HTML="$(substack_curl "$SUBSTACK_PUBLICATION" -sS -H "User-Agent: Mozilla/5.0" "$LIVE_URL")"
        case "$LIVE_HTML" in
          *"$TITLE"*) TITLE_SEEN=1; break ;;
        esac
        echo "self-verify: title not visible yet (attempt $_ATTEMPT/5) $LIVE_URL" >&2
      done
      if [ "$TITLE_SEEN" -ne 1 ]; then
        echo "FATAL: self-verify FAILED — title not found on live page after 5 attempts $LIVE_URL" >&2
        exit 7
      fi
      IMG_COUNT="$(printf '%s' "$LIVE_HTML" | grep -oE 'substack-post-media[^"'"'"']*' | sort -u | wc -l | tr -d ' ')"
      if [ "$IMG_COUNT" -lt 1 ]; then
        echo "FATAL: self-verify FAILED — no substack-post-media image found on live page $LIVE_URL" >&2
        exit 7
      fi
      echo "SELF_VERIFY_OK images_found=$IMG_COUNT url=$LIVE_URL"
      if [ -n "$PAIR" ]; then
        python3 "$DIR/publication-guard.py" reconcile --pair "$PAIR" || exit $?
      fi
      exit 0
    else
      echo "DRAFT ONLY (mode=draft). To publish: enable-publish then --mode go with SUBSTACK_MODE=go." >&2
    fi
    ;;
  enable-publish) touch "$WORK/.SUBSTACK_PUBLISH_ENABLED"; echo "Substack publish ENABLED (sentinel)." ;;
  disable-publish) rm -f "$WORK/.SUBSTACK_PUBLISH_ENABLED"; echo "Substack publish disabled (sentinel removed)" ;;
  *) echo "usage: publish-substack-mermaid.sh publish <md> --title T [--subtitle S] [--mode draft|go] | enable-publish | disable-publish" >&2; exit 2 ;;
esac
