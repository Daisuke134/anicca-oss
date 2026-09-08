#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER="$ROOT/scripts/ensure-note-mcp-runtime.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PROJECT="$TMP/note-mcp"
BIN="$TMP/bin"
mkdir -p "$PROJECT" "$BIN"
touch "$PROJECT/pyproject.toml" "$PROJECT/uv.lock"

cat >"$BIN/uv" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"${UV_CALL_LOG:?}"
mkdir -p .venv/bin
printf '#!/usr/bin/env bash\nexit 0\n' >.venv/bin/python
chmod +x .venv/bin/python
SH
chmod +x "$BIN/uv"

export UV_CALL_LOG="$TMP/uv-calls.log"
UV_BIN="$BIN/uv" bash "$HELPER" "$PROJECT"
[[ -x "$PROJECT/.venv/bin/python" ]]
[[ "$(cat "$UV_CALL_LOG")" == "sync --locked --no-dev" ]]

: >"$UV_CALL_LOG"
UV_BIN="$BIN/uv" bash "$HELPER" "$PROJECT"
[[ ! -s "$UV_CALL_LOG" ]]

rm "$PROJECT/.venv/bin/python"
cat >"$BIN/uv-fail" <<'SH'
#!/usr/bin/env bash
exit 42
SH
chmod +x "$BIN/uv-fail"
if UV_BIN="$BIN/uv-fail" bash "$HELPER" "$PROJECT"; then
  echo "expected bootstrap failure to propagate" >&2
  exit 1
fi

# A failed uv sync may leave .venv/bin/python as a symlink into uv's shared
# interpreter cache. The fallback must unlink only the project link, never
# overwrite the cache target.
PROJECT2="$TMP/note-mcp-symlink"
mkdir -p "$PROJECT2/.venv/bin" "$PROJECT2/src/note_mcp" "$TMP/fallback-site"
touch "$PROJECT2/pyproject.toml" "$PROJECT2/uv.lock"
touch "$PROJECT2/src/note_mcp/__init__.py"
printf 'cache-sentinel\n' >"$TMP/cache-target"
ln -s "$TMP/cache-target" "$PROJECT2/.venv/bin/python"
cat >"$BIN/fallback-python" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-c" ]]; then
  if [[ "$2" == *"site.getsitepackages"* ]]; then
    printf '%s\n' "${FALLBACK_SITE:?}"
  fi
  exit 0
fi
exit 0
SH
chmod +x "$BIN/fallback-python"
FALLBACK_SITE="$TMP/fallback-site" \
  UV_BIN="$BIN/uv-fail" \
  NOTE_MCP_FALLBACK_PYTHON="$BIN/fallback-python" \
  bash "$HELPER" "$PROJECT2"
[[ "$(cat "$TMP/cache-target")" == "cache-sentinel" ]]
[[ ! -L "$PROJECT2/.venv/bin/python" ]]

# Interpreter paths with spaces stay one argv element.
SPACE_BIN="$TMP/bin with space"
mkdir -p "$SPACE_BIN"
cp "$BIN/fallback-python" "$SPACE_BIN/fallback python"
chmod +x "$SPACE_BIN/fallback python"
rm "$PROJECT2/.venv/bin/python"
FALLBACK_SITE="$TMP/fallback-site" \
  UV_BIN="$BIN/uv-fail" \
  NOTE_MCP_FALLBACK_PYTHON="$SPACE_BIN/fallback python" \
  bash "$HELPER" "$PROJECT2"
[[ -x "$PROJECT2/.venv/bin/python" ]]

# A symlinked parent runtime is never mutated or accepted as a project env.
PROJECT3="$TMP/note-mcp-parent-symlink"
mkdir -p "$TMP/real-runtime/.venv/bin" "$PROJECT3/src/note_mcp"
touch "$PROJECT3/pyproject.toml" "$PROJECT3/uv.lock" "$PROJECT3/src/note_mcp/__init__.py"
ln -s "$TMP/real-runtime/.venv" "$PROJECT3/.venv"
cat >"$BIN/uv-fail-log" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf 'called\n' >>"${UV_CALL_LOG:?}"
exit 42
SH
chmod +x "$BIN/uv-fail-log"
if FALLBACK_SITE="$TMP/fallback-site" \
  UV_BIN="$BIN/uv-fail-log" \
  UV_CALL_LOG="$TMP/uv-parent-calls.log" \
  NOTE_MCP_FALLBACK_PYTHON="$BIN/fallback-python" \
  bash "$HELPER" "$PROJECT3"; then
  echo "expected symlinked parent runtime to fail closed" >&2
  exit 1
fi
[[ -L "$PROJECT3/.venv" ]]
[[ ! -e "$TMP/uv-parent-calls.log" ]]

echo "PASS: note runtime bootstraps once and fails closed"
