#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATE="$ROOT/scripts/editorial-gate.sh"
CONTROL="$ROOT/scripts/sol_trigger_control.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAKE="$TMP/fake-model-runner.sh"
cat >"$FAKE" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
if [ "${ARTICLE_MODEL_ROLE:-terra}" = "sol-audit" ]; then
  printf 'sol\n' >>"$CALLS"
  if [ -n "${SOL_RESULT:-}" ]; then
    printf '%s\n' "$SOL_RESULT"
  else
    printf '%s\n' '{"verdict":"PASS","fixes":[],"strengths":[]}'
  fi
else
  printf 'terra\n' >>"$CALLS"
  printf '%s\n' '{"verdict":"PASS","fixes":[],"strengths":[]}'
fi
SH
chmod +x "$FAKE"

seed_four() {
  local base="$1" n article
  mkdir -p "$base"
  for n in 1 2 3 4; do
    article="$base/seed-$n.md"
    printf 'seed %s\n' "$n" >"$article"
    python3 "$CONTROL" quality-sample --state "$base/state.json" \
      --run-id "seed-$n" --artifact-id article-ja --article "$article" \
      --language ja --receipt "$base/seed-$n.json" >/dev/null
  done
}

seed_four "$TMP/pass"
mkdir -p "$TMP/pass/run-5/gates"
printf '# useful article\n' >"$TMP/pass/run-5/article-ja.md"
export ARTICLE_MODEL_RUNNER="$FAKE" CALLS="$TMP/pass/calls"
export ARTICLE_RUN_ID=run-5 ARTICLE_RUN_DIR="$TMP/pass/run-5"
export ARTICLE_SOL_SAMPLE_STATE="$TMP/pass/state.json"
bash "$GATE" "$ARTICLE_RUN_DIR/article-ja.md" --lang ja >/dev/null
[ "$(grep -c '^terra$' "$CALLS")" -eq 1 ]
[ "$(grep -c '^sol$' "$CALLS")" -eq 1 ]
jq -e '.verdict == "PASS" and .article_sha256 != null' \
  "$ARTICLE_RUN_DIR/gates/sol-audit-ja.json" >/dev/null

# Same bytes reuse the durable editorial and Sol audits; neither model reruns.
bash "$GATE" "$ARTICLE_RUN_DIR/article-ja.md" --lang ja >/dev/null
[ "$(grep -c '^terra$' "$CALLS")" -eq 1 ]
[ "$(grep -c '^sol$' "$CALLS")" -eq 1 ]

# The non-selected language never calls Sol for the JA sample slot.
printf '# useful English article\n' >"$ARTICLE_RUN_DIR/article-en.md"
bash "$GATE" "$ARTICLE_RUN_DIR/article-en.md" --lang en >/dev/null
[ "$(grep -c '^sol$' "$CALLS")" -eq 1 ]

# A Sol FAIL blocks that attempt. A changed repair gets Terra PASS without a
# second Sol purchase, while the old Sol defect receipt remains durable.
seed_four "$TMP/fail"
mkdir -p "$TMP/fail/run-5/gates"
printf '# first draft\n' >"$TMP/fail/run-5/article-ja.md"
export CALLS="$TMP/fail/calls" ARTICLE_RUN_DIR="$TMP/fail/run-5"
export ARTICLE_SOL_SAMPLE_STATE="$TMP/fail/state.json"
export SOL_RESULT='{"verdict":"FAIL","fixes":["repair evidence"],"strengths":[]}'
if bash "$GATE" "$ARTICLE_RUN_DIR/article-ja.md" --lang ja >/dev/null; then
  echo "expected sampled Sol FAIL to block" >&2
  exit 1
fi
printf '# repaired draft with evidence\n' >"$ARTICLE_RUN_DIR/article-ja.md"
unset SOL_RESULT
bash "$GATE" "$ARTICLE_RUN_DIR/article-ja.md" --lang ja >/dev/null
[ "$(grep -c '^terra$' "$CALLS")" -eq 2 ]
[ "$(grep -c '^sol$' "$CALLS")" -eq 1 ]

# A claimed receipt without a matching audit is an interrupted audit and must
# fail closed before either a replayed Sol call or publication-quality PASS.
seed_four "$TMP/interrupted"
mkdir -p "$TMP/interrupted/run-5/gates"
printf '# interrupted draft\n' >"$TMP/interrupted/run-5/article-ja.md"
python3 "$CONTROL" quality-sample --state "$TMP/interrupted/state.json" \
  --run-id run-5 --artifact-id article-ja \
  --article "$TMP/interrupted/run-5/article-ja.md" --language ja \
  --receipt "$TMP/interrupted/run-5/gates/sol-trigger-ja.json" >/dev/null
mkdir "$TMP/interrupted/run-5/gates/sol-trigger-ja.json.claim"
export CALLS="$TMP/interrupted/calls" ARTICLE_RUN_DIR="$TMP/interrupted/run-5"
export ARTICLE_SOL_SAMPLE_STATE="$TMP/interrupted/state.json"
if bash "$GATE" "$ARTICLE_RUN_DIR/article-ja.md" --lang ja >/dev/null 2>&1; then
  echo "expected interrupted Sol audit to fail closed" >&2
  exit 1
fi
[ "$(grep -c '^terra$' "$CALLS")" -eq 1 ]
! grep -q '^sol$' "$CALLS"

echo "PASS: editorial gate owns deterministic one-use Sol sampling"
