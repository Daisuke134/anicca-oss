#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATE="$ROOT/scripts/editorial-gate.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/run/gates"

cat >"$TMP/bin/model-runner" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
test "$1" = judge
test "$2" = --prompt-file
test "$3" = -
cat >"$CAPTURED_PROMPT"
printf '%s\n' \
  '{"verdict":"FAIL","fixes":["末尾の実行記録プロダクトへの宣伝リンクと文を削除する。","見出しを具体化する。"],"strengths":["実測値がある。"]}'
EOF
chmod +x "$TMP/bin/model-runner"

export ARTICLE_MODEL_RUNNER="$TMP/bin/model-runner"
export ARTICLE_RUN_DIR="$TMP/run"
export CAPTURED_PROMPT="$TMP/prompt.txt"
export ARTICLE_PRODUCT_LANDING_URL=https://writer.example/product
ARTICLE="$ARTICLE_RUN_DIR/article-ja.md"
cat >"$ARTICLE" <<'EOF'
# 実測結果

検索結果を比較した。

[次の実行を記録する](https://writer.example/product?product_id=fixture&run_id=daily-cta-contract&artifact_id=article-ja&variant_id=measured-cta&click_id=daily-cta-contract-article-ja)
EOF
BEFORE_HASH="$(shasum -a 256 "$ARTICLE" | awk '{print $1}')"

set +e
bash "$GATE" "$ARTICLE" --lang ja >"$TMP/out.json" 2>"$TMP/err"
rc=$?
set -e
[ "$rc" -eq 1 ]
[ "$(shasum -a 256 "$ARTICLE" | awk '{print $1}')" = "$BEFORE_HASH" ]

grep -q 'exactly one measurable product CTA is a mandatory' "$CAPTURED_PROMPT"
grep -q 'revenue-path invariant' "$CAPTURED_PROMPT"
jq -e '
  .verdict == "FAIL"
  and (.fixes | length == 2)
  and (.fixes[0] | contains("Keep exactly one mandatory measurable product CTA"))
  and (.fixes[0] | contains("削除") | not)
  and .fixes[1] == "見出しを具体化する。"
  and .editorial_contract_reconciled == ["mandatory_measurable_cta_preserved"]
' "$TMP/out.json" >/dev/null
jq -e '
  .fixes[0] | contains("Keep exactly one mandatory measurable product CTA")
' "$ARTICLE_RUN_DIR/gates/editorial-ja.json" >/dev/null

printf 'PASS: editorial gate preserves the mandatory measurable CTA without faking PASS\n'
