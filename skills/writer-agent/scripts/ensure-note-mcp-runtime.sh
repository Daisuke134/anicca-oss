#!/usr/bin/env bash
# Restore note-mcp's locked project environment when a fresh clone has no .venv.
set -euo pipefail

NOTE_MCP_DIR="${1:?usage: ensure-note-mcp-runtime.sh NOTE_MCP_DIR}"
PY_VENV="$NOTE_MCP_DIR/.venv/bin/python"
if [[ -L "$NOTE_MCP_DIR/.venv" || -L "$NOTE_MCP_DIR/.venv/bin" ]]; then
  echo "FATAL: refusing symlinked note-mcp runtime parent at $NOTE_MCP_DIR/.venv" >&2
  exit 6
fi
if [[ -x "$PY_VENV" ]] && "$PY_VENV" -c 'import fastmcp, note_mcp, pathlib, sys; expected = pathlib.Path(sys.argv[1]).resolve(); actual = pathlib.Path(note_mcp.__file__).resolve(); assert expected in actual.parents, (expected, actual)' "$NOTE_MCP_DIR/src/note_mcp" >/dev/null 2>&1; then
  exit 0
fi

[[ -f "$NOTE_MCP_DIR/pyproject.toml" ]] || {
  echo "FATAL: note-mcp pyproject.toml missing at $NOTE_MCP_DIR" >&2
  exit 2
}
[[ -f "$NOTE_MCP_DIR/uv.lock" ]] || {
  echo "FATAL: note-mcp uv.lock missing at $NOTE_MCP_DIR" >&2
  exit 2
}

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
[[ -x "$UV_BIN" ]] || {
  echo "FATAL: uv is required to restore note-mcp runtime" >&2
  exit 6
}

echo "note-mcp runtime missing; restoring from uv.lock" >&2

restore_shared_runtime() {
  local fallback fallback_site
  fallback="${NOTE_MCP_FALLBACK_PYTHON:-${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}}"
  [[ -x "$fallback" ]] || return 1
  [[ -f "$NOTE_MCP_DIR/src/note_mcp/__init__.py" ]] || return 1
  fallback_site="$("$fallback" -c 'import site; print(site.getsitepackages()[0])')" || return 1
  PYTHONPATH="$NOTE_MCP_DIR/src:$fallback_site${PYTHONPATH:+:$PYTHONPATH}" \
    "$fallback" -c 'import fastmcp, note_mcp, pathlib, sys; expected = pathlib.Path(sys.argv[1]).resolve(); actual = pathlib.Path(note_mcp.__file__).resolve(); assert expected in actual.parents, (expected, actual)' \
    "$NOTE_MCP_DIR/src/note_mcp" >/dev/null 2>&1 || return 1

  mkdir -p "$(dirname "$PY_VENV")"
  # uv may leave .venv/bin/python as a symlink to its interpreter cache. Do
  # not follow that link when installing the fallback wrapper.
  [[ -L "$PY_VENV" ]] && unlink "$PY_VENV"
  cat >"$PY_VENV" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
NOTE_MCP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
FALLBACK="${NOTE_MCP_FALLBACK_PYTHON:-${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}}"
FALLBACK_SITE="$("$FALLBACK" -c 'import site; print(site.getsitepackages()[0])')"
export PYTHONPATH="$NOTE_MCP_DIR/src:$FALLBACK_SITE${PYTHONPATH:+:$PYTHONPATH}"
exec "$FALLBACK" "$@"
SH
  chmod +x "$PY_VENV"
  echo "note-mcp runtime: using validated shared fallback $fallback" >&2
}

if ! (
  cd "$NOTE_MCP_DIR"
  # The worker only needs the runtime dependency group. Installing the
  # development group pulls mitmproxy/zstandard and can fail before Note
  # readback is even possible on a clean host.
  "$UV_BIN" sync --locked --no-dev
); then
  echo "note-mcp runtime: uv sync unavailable; trying shared runtime" >&2
  restore_shared_runtime || {
    echo "FATAL: neither locked runtime nor validated shared fallback is available" >&2
    exit 6
  }
fi

[[ -x "$PY_VENV" ]] || {
  echo "FATAL: uv sync completed without creating $PY_VENV" >&2
  exit 6
}
