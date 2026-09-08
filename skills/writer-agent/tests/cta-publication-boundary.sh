#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HELPER="$ROOT/skills/writer-agent/scripts/publication_resume.py"
MEDIA="$ROOT/skills/writer-agent/scripts/canonical_media.py"
TMP="$(mktemp -d /tmp/article-cta-boundary.XXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT
export NOTE_URLNAME=writer-note
export ZENN_ACCOUNT=writer-zenn
export DEVTO_ACCOUNT_HANDLE=writer-devto
export SUBSTACK_PUBLICATION_JA=writer-ja.substack.com
export SUBSTACK_PUBLICATION_EN=writer-en.substack.com
export X_ACCOUNT_HANDLE=writer-x
export ARTICLE_PRODUCT_LANDING_URL=https://writer.example/product

make_run() {
  local run_dir="$1"
  local missing_artifact="$2"
  local run_id
  run_id="$(basename "$run_dir")"
  mkdir -p "$run_dir/gates"
  printf '# JA\n\n本文\n' >"$run_dir/article-ja.md"
  printf '%s\n' '---' 'title: "CTA fixture"' 'tags: ai, agents' '---' '# EN' '' 'Body' \
    >"$run_dir/article-en.md"
  printf '短文\n' >"$run_dir/x-post-ja.txt"
  [ "$missing_artifact" = ja ] || printf '\nhttps://writer.example/product?product_id=writer&run_id=%s&artifact_id=article-ja&variant_id=fixture-ja&click_id=%s-article-ja\n' "$run_id" "$run_id" >>"$run_dir/article-ja.md"
  [ "$missing_artifact" = en ] || printf '\nhttps://writer.example/product?product_id=writer&run_id=%s&artifact_id=article-en&variant_id=fixture-en&click_id=%s-article-en\n' "$run_id" "$run_id" >>"$run_dir/article-en.md"
  [ "$missing_artifact" = x-post-ja ] || printf '\nhttps://writer.example/product?product_id=writer&run_id=%s&artifact_id=x-post-ja&variant_id=fixture-x&click_id=%s-x-post-ja\n' "$run_id" "$run_id" >>"$run_dir/x-post-ja.txt"
  python3 "$MEDIA" attach --file "$run_dir/article-ja.md" >/dev/null
  python3 "$MEDIA" attach --file "$run_dir/article-en.md" >/dev/null
  printf 'headline\n' >"$run_dir/headline-image.png"
  printf 'diagram\n' >"$run_dir/body-diagram.png"
}

init_run() {
  local run_dir="$1"
  python3 "$HELPER" \
    --state "$run_dir/gates/publication-state.json" \
    --ledger "$TMP/state/articles.jsonl" init \
    --run-id "$(basename "$run_dir")" \
    --run-dir "$run_dir" \
    --topic-id cta-boundary-fixture \
    --draft-ja "$run_dir/article-ja.md" \
    --draft-en "$run_dir/article-en.md" \
    --x-post-ja "$run_dir/x-post-ja.txt" \
    --headline-image "$run_dir/headline-image.png" \
    --body-asset "$run_dir/body-diagram.png" \
    --safety ALLOW \
    --max-resume-attempts 2
}

day=27
for missing_artifact in ja en x-post-ja; do
  MISSING="$TMP/state/runs/daily-2026-07-$day"
  make_run "$MISSING" "$missing_artifact"
  set +e
  init_run "$MISSING" >"$TMP/missing-$missing_artifact.out" 2>&1
  missing_rc=$?
  set -e
  test "$missing_rc" -ne 0
  test ! -e "$MISSING/gates/publication-state.json"
  test -f "$MISSING/gates/cta-$missing_artifact.json" || {
    printf 'FAIL: CTA evidence missing for %s\n' "$missing_artifact" >&2
    sed -n '1,80p' "$TMP/missing-$missing_artifact.out" >&2
    exit 1
  }
  jq -e '.gate == "cta" and .verdict == "FAIL"' \
    "$MISSING/gates/cta-$missing_artifact.json" >/dev/null
  day=$((day + 1))
done

VALID="$TMP/state/runs/daily-2026-07-30"
make_run "$VALID" none
init_run "$VALID" >"$TMP/valid.out"
test -s "$VALID/gates/publication-state.json"
for artifact in ja en x-post-ja; do
  jq -e '.gate == "cta" and .verdict == "PASS"' \
    "$VALID/gates/cta-$artifact.json" >/dev/null
done

echo 'PASS: publication init blocks every CTA-less artifact before state or publishing'
