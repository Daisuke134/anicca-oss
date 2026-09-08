#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/weekly-audit-path.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/skill/scripts"

cat >"$TMP/skill/scripts/article_weekly_audit.py" <<'PY'
import json
import sys

print(json.dumps({
    "python": sys.executable,
}))
PY

output="$(
  env -i \
    HOME="$HOME" \
    PATH="/usr/bin:/bin:/usr/sbin:/sbin" \
    LIFE_MANAGER_REPO="$ROOT/../.." \
    LIFE_MANAGER_ENV_FILE="$TMP/missing.env" \
    LIFE_MANAGER_PYTHON="/usr/bin/python3" \
    WRITER_STATE_DIR="$TMP/state" \
    ARTICLE_SKILL_DIR="$TMP/skill" \
    /bin/bash "$ROOT/scripts/audit-7day.sh"
)"

/usr/bin/python3 - "$output" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
assert value["python"].endswith("/usr/bin/python3"), value
print("PASS: weekly audit wrapper uses the managed Python without OpenClaw")
PY
