#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

touch "$TMP/state-is-a-file" "$TMP/article.md" "$TMP/cover.png" "$TMP/env"
cat >"$TMP/fake-python" <<EOF
#!/usr/bin/env bash
touch "$TMP/consumer-ran"
EOF
chmod +x "$TMP/fake-python"

set +e
WRITER_STATE_DIR="$TMP/state-is-a-file" \
LIFE_MANAGER_ENV_FILE="$TMP/env" \
LIFE_MANAGER_PYTHON="$TMP/fake-python" \
WRITER_BROWSER_PYTHON="$TMP/fake-python" \
bash "$ROOT/skills/writer-agent/scripts/note-publish/publish-to-note.sh" \
  publish "$TMP/article.md" --eyecatch "$TMP/cover.png" >"$TMP/output" 2>&1
status=$?
set -e

test "$status" -ne 0
test ! -e "$TMP/consumer-ran"
grep -Fq "ERROR: failed to stage eyecatch in Writer state" "$TMP/output"
echo "note eyecatch staging fail-closed: PASS"
