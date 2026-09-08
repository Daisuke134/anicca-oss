#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/skill/scripts" "$TMP/state" "$TMP/run/gates" \
  "$TMP/home" "$TMP/bin"
cp "$ROOT/scripts/run.sh" "$TMP/skill/scripts/run.sh"
cp "$ROOT/scripts/writer-runtime-env.sh" "$TMP/skill/scripts/writer-runtime-env.sh"
cat >"$TMP/skill/scripts/quality-phase-terminal.py" <<'PY'
raise SystemExit(0)
PY
cat >"$TMP/skill/scripts/publish-note.sh" <<'SH'
#!/usr/bin/env bash
printf 'DRAFT (unpublished) https://note.test/draft stage1_ok=true stage2_ok=true\n'
SH
cat >"$TMP/skill/scripts/propose.sh" <<'SH'
#!/usr/bin/env bash
printf '{"pattern": {"source_id": "test", "niche_tags": ["test"]}}\n'
SH
cat >"$TMP/bin/curl" <<'SH'
#!/usr/bin/env bash
printf '404'
SH
chmod +x "$TMP/skill/scripts/"*.sh "$TMP/bin/curl"
printf '# test\n' >"$TMP/article.md"

HOME="$TMP/home" PATH="$TMP/bin:$PATH" \
  LIFE_MANAGER_REPO="$(cd "$ROOT/../.." && pwd)" \
  LIFE_MANAGER_ENV_FILE="$TMP/home/missing.env" NOTE_URLNAME="test-writer" \
  ARTICLE_SKILL_DIR="$TMP/skill" ARTICLE_STATE_DIR="$TMP/state" \
  ARTICLE_RUN_DIR="$TMP/run" ARTICLE_QUALITY_ADVISORY=1 \
  bash "$TMP/skill/scripts/run.sh" --channel note --phase publish \
    --markdown-file "$TMP/article.md" --title "題" --meta "説明" >/dev/null

test -f "$TMP/state/note-"*.meta.json
test ! -e "$TMP/skill/state"
echo "PASS: run.sh archives managed metadata in ARTICLE_STATE_DIR"
