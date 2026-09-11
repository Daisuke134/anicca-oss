#!/usr/bin/env bash
set -euo pipefail

expected_h1='# Life Manager'
english_identity='Life Manager is the product. Anicca is the company name only when a form explicitly asks for it.'
japanese_identity='Life Managerが製品名です。Aniccaはformが会社名を明示的に求めた時だけ使います。'
separate_en='separate pro''ject|its own re''po'
separate_ja='独立したプロ''ジェクト|このリポジトリには含まれま''せん|このrepoに含まれま''せん'
identity_contradiction_pattern="${separate_en}|${separate_ja}"

test "$(grep -m1 '^# ' README.md)" = "$expected_h1" || {
  echo 'wrong README.md H1' >&2
  exit 1
}
test "$(grep -m1 '^# ' README.ja.md)" = "$expected_h1" || {
  echo 'wrong README.ja.md H1' >&2
  exit 1
}
git grep -Fq "$english_identity" -- README.md || {
  echo 'missing English Life Manager identity boundary' >&2
  exit 1
}
git grep -Fq "$japanese_identity" -- README.ja.md || {
  echo 'missing Japanese Life Manager identity boundary' >&2
  exit 1
}
# Anicca may appear as the company name or as an owned app name such as Anicca iOS.
# The explicit identity boundary above and the contradiction scan below prevent it
# from being presented as a second product or repository.
if grep -Fq 'アニッチャ' README.ja.md; then
  echo 'legacy AI name remains in README.ja.md' >&2
  exit 1
fi
for product_identity_file in \
  THESIS.md docs/EXECUTION-ORDER.md install.sh runtime/loop/ledger-publish.mjs; do
  if grep -Fq 'Anicca' "$product_identity_file"; then
    echo "legacy product/AI name remains: $product_identity_file" >&2
    exit 1
  fi
done
if grep -Eqi 'anicca (loop|install)' install.sh; then
  echo 'legacy lowercase product name remains in install.sh output' >&2
  exit 1
fi
# Published external asset identifiers are immutable legacy namespace, not a
# second repository or runtime source dependency.
if git grep -nI -E "$identity_contradiction_pattern" -- README.md README.ja.md; then
  echo 'README still describes Life Manager as a separate repository' >&2
  exit 1
fi
test "$(grep -Fxc -- '- **Repository (whole product):** <https://github.com/Daisuke134/life-manager>' README.md)" = 1
test "$(grep -Fxc -- '- **リポジトリ（プロダクト全体）：** <https://github.com/Daisuke134/life-manager>' README.ja.md)" = 1

legacy_repo='anic''ca'
legacy_pattern="github\\.com/Daisuke134/${legacy_repo}([^[:alnum:]_-]|$)|raw\\.githubusercontent\\.com/Daisuke134/${legacy_repo}([^[:alnum:]_-]|$)|daisuke134\\.github\\.io/${legacy_repo}([^[:alnum:]_-]|$)"

if git grep -nI -E "$legacy_pattern" -- . \
  ':(exclude)docs/superpowers/specs/**' \
  ':(exclude)docs/superpowers/plans/**' \
  ':(exclude)specs/archive/**' \
  ':(exclude).vcsdd/**' \
  ':(exclude)**/logs/**'; then
  echo 'legacy live repository URL remains' >&2
  exit 1
fi

for live_file in \
  README.md README.ja.md THESIS.md docs/ARTICLE-LAUNCH-TODO.md \
  docs/EXECUTION-ORDER.md install.sh runtime/loop/ledger-publish.mjs \
  skills/earn/x402-sell/chip-metadata.json skills/earn/x402-sell/chip.json \
  skills/self/cadence-known-gaps.json skills/self/spawn-child/sdl/child.yaml \
  skills/self/spawn/scripts/deploy-akash.sh; do
  git grep -q 'Daisuke134/life-manager' -- "$live_file" || {
    echo "missing final repository URL: $live_file" >&2
    exit 1
  }
done
