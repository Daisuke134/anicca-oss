#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
source "$ROOT/skills/writer-agent/tests/_exact8-fixture.sh"
CONTROL="$ROOT/skills/writer-agent/scripts/zenn-deferred-control.py"
COMPLETE="$ROOT/skills/writer-agent/scripts/article-run-complete.py"
TMP="$(mktemp -d /tmp/article-completion-validator.XXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT
RUN=run-validator
LEDGER="$TMP/articles.jsonl"
ARTIFACT="$TMP/runs/$RUN/gates/zenn-deferred.json"
STATE="$TMP/runs/$RUN/gates/publication-state.json"
REPO="$TMP/repo"
mkdir -p "$(dirname "$ARTIFACT")" "$REPO/articles"

emit() {
  printf '{"run_id":"%s","topic_id":"%s","platform":"%s","lang":"%s","live_url":"%s","published":true,"reality_gate":"PASS"}\n' \
    "$RUN" "$1" "$2" "$3" "$4" >>"$LEDGER"
}

printf '%s\n' '---' 'title: "Strict title"' 'published: true' '---' >"$REPO/articles/strict-slug-1.md"
python3 "$CONTROL" create --artifact "$ARTIFACT" --markdown-file "$REPO/articles/strict-slug-1.md" --slug strict-slug-1
exact8_init_state "$ROOT" "$TMP/runs/$RUN" "$LEDGER" "$RUN" topic-1 strict-slug-1
exact8_record_other_seven "$ROOT" "$TMP/runs/$RUN" "$LEDGER"

# Artifact source and URL are canonical, not merely matching by basename.
python3 - "$ARTIFACT" <<'PY'
import json, sys
p=sys.argv[1]; x=json.load(open(p)); x["live_url"]="https://evil.test/anicca/articles/strict-slug-1"
json.dump(x, open(p,"w"));
PY
if python3 "$CONTROL" handoff --repo "$REPO" --ledger "$LEDGER" --run-id "$RUN" --artifact "$ARTIFACT" 2>/dev/null; then
  echo 'FAIL: evil artifact URL was accepted' >&2
  exit 1
fi

# The final exact-eight validator starts from one known-good ledger, then
# mutates one invariant at a time so each rejection has only one cause.
printf '{"run_id":"%s","topic_id":"topic-1","platform":"zenn-article","lang":"ja","live_url":"https://zenn.dev/writer-zenn/articles/strict-slug-1","published":true,"reality_gate":"PASS"}\n' "$RUN" >>"$LEDGER"
python3 "$COMPLETE" --ledger "$LEDGER" --run-id "$RUN" --armed 1 --publication-state "$STATE"
cp "$LEDGER" "$TMP/valid-eight.jsonl"

sed 's/"topic_id":"topic-1","platform":"zenn-article"/"topic_id":"topic-2","platform":"zenn-article"/' \
  "$TMP/valid-eight.jsonl" >"$TMP/mutated-ledger.jsonl"
mv "$TMP/mutated-ledger.jsonl" "$LEDGER"
if python3 "$COMPLETE" --ledger "$LEDGER" --run-id "$RUN" --armed 1 --publication-state "$STATE"; then
  echo 'FAIL: mixed-topic exact-eight passed' >&2
  exit 1
fi
cp "$TMP/valid-eight.jsonl" "$LEDGER"
sed 's#https://note.com/writer-note/n/note-current#javascript:alert(1)#' \
  "$TMP/valid-eight.jsonl" >"$TMP/mutated-ledger.jsonl"
mv "$TMP/mutated-ledger.jsonl" "$LEDGER"
if python3 "$COMPLETE" --ledger "$LEDGER" --run-id "$RUN" --armed 1 --publication-state "$STATE"; then
  echo 'FAIL: invalid live URL passed' >&2
  exit 1
fi

echo 'PASS: handoff and completion share strict evidence validation'
