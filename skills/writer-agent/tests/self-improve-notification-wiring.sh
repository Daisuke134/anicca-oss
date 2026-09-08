#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/self-improve-notify.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
SKILL="$TMP/skill"
mkdir -p "$SKILL/scripts" "$SKILL/state/learning" "$TMP/home/.openclaw/logs"
: >"$TMP/life-manager.env"

for helper in score-latest-run.sh topic-supply.sh; do
  printf '#!/usr/bin/env bash\nexit 0\n' >"$SKILL/scripts/$helper"
  chmod +x "$SKILL/scripts/$helper"
done

cat >"$SKILL/scripts/writer_learning_worker.py" <<'PY'
import json
import os
import sys
from pathlib import Path
command = sys.argv[1]
with Path(os.environ["LEARNING_CALLS"]).open("a", encoding="utf-8") as handle:
    handle.write(command + "\n")
if command == "close-canary":
    print(json.dumps({"status": "NO_APPLIED_CANARY"}))
elif command == "offline":
    print(json.dumps({
        "schema_version": 2,
        "status": "AWAITING_MATCHED_CANARY",
        "experiment_id": "learning-2026-07-28",
        "replay_receipts": 6,
    }))
else:
    raise SystemExit(f"unexpected command: {command}")
PY

cat >"$SKILL/scripts/self_improve_control.py" <<'PY'
import json
print(json.dumps({"checked_run": "daily-2026-07-27", "missing_count": 0}))
PY

cat >"$SKILL/scripts/writer_report_worker.py" <<'PY'
import json
import os
from pathlib import Path
Path(os.environ["NOTIFY_MARKER"]).write_text("learning-report\n", encoding="utf-8")
print(json.dumps({"status": "sent", "message_ids": ["fixture-42"]}))
PY

if ! NOTIFY_MARKER="$TMP/notified" \
  LEARNING_CALLS="$TMP/learning-calls" \
  HOME="$TMP/home" \
  LIFE_MANAGER_REPO="$(cd "$ROOT/../.." && pwd)" \
  LIFE_MANAGER_ENV_FILE="$TMP/life-manager.env" \
  ARTICLE_SKILL_DIR="$SKILL" \
  bash "$ROOT/scripts/self-improve.sh" >"$TMP/stdout" 2>"$TMP/stderr"; then
  cat "$TMP/stdout" >&2
  cat "$TMP/stderr" >&2
  exit 1
fi
if [[ "$(cat "$TMP/learning-calls")" != $'close-canary\noffline' ]]; then
  cat "$TMP/learning-calls" >&2
  echo "learning close/offline order is wrong" >&2
  exit 1
fi

if [[ ! -f "$TMP/notified" ]] || [[ "$(cat "$TMP/notified")" != "learning-report" ]]; then
  cat "$TMP/stdout" >&2
  cat "$TMP/stderr" >&2
  echo "learning report marker missing or mismatched" >&2
  exit 1
fi
if ! grep -q '"message_ids": \["fixture-42"\]' "$TMP/stdout"; then
  cat "$TMP/stdout" >&2
  echo "notification receipt missing from stdout" >&2
  exit 1
fi
echo "PASS: 22:30 wrapper reports replay-first learning receipt"
