#!/usr/bin/env bash
# seo-gate.sh — Mandatory SEO + persona gate before any article publish
# Spec: ANICCA_USEFUL_CONTENT_SPEC.md T2-25 (HR-A useful + HR-H persona NG)
# Usage:
#   bash seo-gate.sh --title <t> --meta <m> --markdown-file <f> --lang <ja|en>
# Exit 0 = pass. Exit !=0 = fix and retry.

set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"

TITLE=""
META=""
MD_FILE=""
LANG=""
ENFORCE_UNIQUE=0
IS_PAID_BODY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --title)          TITLE="$2"; shift 2 ;;
    --meta)           META="$2"; shift 2 ;;
    --markdown-file)  MD_FILE="$2"; shift 2 ;;
    --lang)           LANG="$2"; shift 2 ;;
    --enforce-unique) ENFORCE_UNIQUE=1; shift ;;
    --is-paid-body)   IS_PAID_BODY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$TITLE" && -n "$META" && -n "$MD_FILE" && -n "$LANG" ]] || { echo "FATAL: --title --meta --markdown-file --lang all required" >&2; exit 1; }
[[ -f "$MD_FILE" ]] || { echo "FATAL: markdown file not found: $MD_FILE" >&2; exit 1; }

MD="$(cat "$MD_FILE")"
ERRORS=0
WARN=0

configured_url_present() {
  local configured="$1" item
  local -a items
  IFS=',' read -r -a items <<< "$configured"
  for item in "${items[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [[ "$item" == https://* || "$item" == http://* ]] || continue
    if printf '%s' "$MD" | grep -F -q -- "$item"; then
      return 0
    fi
  done
  return 1
}

# Title length
TL="$(printf '%s' "$TITLE" | python3 -c "import sys; print(len(sys.stdin.read()))")"
case "$LANG" in
  ja)
    [[ "$TL" -ge 32 && "$TL" -le 60 ]] || { echo "❌ title length: $TL chars (need 32-60 for JA)" >&2; ERRORS=$((ERRORS+1)); } ;;
  en)
    [[ "$TL" -ge 50 && "$TL" -le 70 ]] || { echo "❌ title length: $TL chars (need 50-70 for EN)" >&2; ERRORS=$((ERRORS+1)); } ;;
esac

# Meta description length. meta IS run.sh's --meta argument, NOT the frontmatter
# `description:` field -- easy to confuse (team-lead hit this 2026-07-16), say so in the FATAL.
ML="$(printf '%s' "$META" | python3 -c "import sys; print(len(sys.stdin.read()))")"
[[ "$ML" -ge 120 && "$ML" -le 156 ]] || { echo "❌ meta length: $ML chars (need 120-156). meta is run.sh's --meta argument, not the markdown frontmatter's description field." >&2; ERRORS=$((ERRORS+1)); }

# H2 count (markdown ## ). Range widened 3-7 -> 3-12 (task #14): the real product is a
# long-form explainer, not a short app-funnel post -- an upper bound of 7 was killing real
# articles (the agent-economy piece has 11 H2s). Lower bound 3 unchanged (still needs structure).
H2="$(printf '%s' "$MD" | grep -c '^## ' || true)"
[[ "$H2" -ge 3 && "$H2" -le 12 ]] || { echo "❌ H2 count: $H2 (need 3-12)" >&2; ERRORS=$((ERRORS+1)); }

# Installation-owned destinations, supplied as comma-separated absolute URL prefixes.
INTERNAL_URLS="${ARTICLE_INTERNAL_LINK_URLS:-${ARTICLE_CTA_URLS:-}}"
if [[ -z "$INTERNAL_URLS" ]]; then
  echo "❌ ARTICLE_INTERNAL_LINK_URLS or ARTICLE_CTA_URLS is required" >&2
  ERRORS=$((ERRORS+1))
elif configured_url_present "$INTERNAL_URLS"; then
  INT_LINK=1
else
  INT_LINK=0
  echo "❌ internal link count: 0 (need ≥1 configured installation URL)" >&2
  ERRORS=$((ERRORS+1))
fi

# CTA-link requirement (task #14, replaces the old iOS-app-era aniccaai.com-anchor +
# App Store deeplink requirement -- that funnel is gone; today's funnel is free version ->
# note paid version). Body must link where the reader actually goes next: note (the paid
# full version), Substack, or aniccaai.com. NOT restricted to markdown [text](url) syntax --
# the real free-version generator (make-free-version.py) emits the note CTA as a bare
# trailing URL on its own line (so note.com auto-embeds a rich card), which a bracket-only
# regex would miss. Exempt with --is-paid-body: a paid full version's CTA is itself, the
# reader is already there.
if [[ "$IS_PAID_BODY" -eq 0 ]]; then
  CTA_URLS="${ARTICLE_CTA_URLS:-}"
  if [[ -z "$CTA_URLS" ]] || ! configured_url_present "$CTA_URLS"; then
    echo "❌ seo-gate CTA-link: no configured ARTICLE_CTA_URLS prefix in body (pass --is-paid-body to exempt a paid full version)" >&2
    ERRORS=$((ERRORS+1))
  fi
fi

# persona NG: アニッカ
if printf '%s' "$MD$TITLE$META" | grep -F -- 'アニッカ' >/dev/null 2>&1; then
  echo "❌ persona P-1 violation: 'アニッカ' (must be アニッチャ)" >&2
  ERRORS=$((ERRORS+1))
fi

# persona NG: 'on behalf of'
if printf '%s' "$MD$TITLE$META" | grep -F -i -- 'on behalf of' >/dev/null 2>&1; then
  echo "❌ persona P-2 violation: 'on behalf of' (Anicca speaks for herself)" >&2
  ERRORS=$((ERRORS+1))
fi

# HR-J verbatim borrowed phrase blacklist
VG_LIB="$LIFE_MANAGER_REPO/skills/_shared/lib/verbatim-guard.sh"
if [[ -f "$VG_LIB" ]]; then
  . "$VG_LIB"
  if ! vg_check "${MD}${TITLE}${META}" 2>/tmp/seo-gate-vg.err; then
    echo "❌ HR-J verbatim borrowed phrase detected:" >&2
    cat /tmp/seo-gate-vg.err >&2
    ERRORS=$((ERRORS+1))
  fi
  rm -f /tmp/seo-gate-vg.err
fi

# NG generic phrases (warning, not error)
for ng in "let's dive into" "in today's fast-paced world" "buckle up" "spoiler alert"; do
  if printf '%s' "$MD$TITLE" | grep -i -F -- "$ng" >/dev/null 2>&1; then
    echo "⚠ AI cliché detected: '$ng'" >&2
    WARN=$((WARN+1))
  fi
done

# em-dash overuse: more than 5 em-dash in body = warning
EMDASH="$(printf '%s' "$MD" | grep -c -- '—' || true)"
[[ "$EMDASH" -le 5 ]] || { echo "⚠ em-dash overuse: $EMDASH (≤5)" >&2; WARN=$((WARN+1)); }

# Body min/max word count (rough estimate by char count)
BC="$(printf '%s' "$MD" | python3 -c "import sys; print(len(sys.stdin.read()))")"
case "$LANG" in
  ja) [[ "$BC" -ge 1500 ]] || { echo "❌ body too short: $BC chars (need ≥1500 for JA)" >&2; ERRORS=$((ERRORS+1)); } ;;
  en) [[ "$BC" -ge 4000 ]] || { echo "❌ body too short: $BC chars (need ≥4000 for EN ~800 words)" >&2; ERRORS=$((ERRORS+1)); } ;;
esac

# WS3a A4: unique-word gate(EN, --enforce-unique 時のみ、初期 WARN-first、corpus fallback は flag 無しで exempt)
if [[ "$LANG" == "en" ]]; then
  UNIQ="$(printf '%s' "$MD" | python3 -c "import sys,re; t=re.findall(r\"[a-zA-Z']+\", sys.stdin.read().lower()); print(len(set(t)))")"
  echo "unique_words=$UNIQ (en)"
  if [[ "${ENFORCE_UNIQUE:-0}" -eq 1 && "$UNIQ" -lt 500 ]]; then
    echo "⚠ unique-word below 500: $UNIQ (brief path target ≥500)" >&2
    WARN=$((WARN+1))
  fi
fi

echo "seo-gate: errors=$ERRORS warnings=$WARN title=$TL chars meta=$ML chars H2=$H2 int_links=${INT_LINK:-0} body=$BC chars"
if [[ "$ERRORS" -gt 0 ]]; then
  exit 1
fi
exit 0
