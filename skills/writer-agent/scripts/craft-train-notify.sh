#!/usr/bin/env bash
# craft-train-notify.sh — one Telegram line per night about the craft trainer.
#
# Reads the last row of state/craft-train.jsonl and reports what the trainer
# did. Kept separate from craft-train.sh on purpose: a notifier that lives
# inside the trainer can take the trainer down with it, and the one message
# that matters most is the one about a night that went wrong.
#
# Reports three shapes, because they mean different things and collapsing them
# would hide the interesting one:
#   guard    - refused to train (not enough scored runs). A normal early night.
#   reject   - proposed edits, held-out did not improve, craft file unchanged.
#   accept   - edits kept, with the beat rate before and after.
# A run that changed CRAFT.md without improving the score would be a bug, so
# the sha256 pair is always included: it is the only field that cannot lie
# about whether the file moved.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"
REPO_ROOT="$LIFE_MANAGER_REPO"
SKILL_DIR="$WRITER_ROOT"
JSONL="${CRAFT_TRAIN_JSONL:-$STATE_DIR/craft-train.jsonl}"
TARGET="${CRAFT_TRAIN_TELEGRAM:-}"
PY="${ARTICLE_PYTHON:-$WRITER_BROWSER_PYTHON}"

[ -f "$JSONL" ] || { echo "craft-train-notify: no $JSONL yet"; exit 0; }

MESSAGE="$("$PY" - "$JSONL" <<'PYEOF'
import json, sys
from pathlib import Path

rows = [l for l in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if l.strip()]
if not rows:
    print("")
    raise SystemExit(0)
row = json.loads(rows[-1])

before = row.get("craft_sha256_before") or ""
after = row.get("craft_sha256_after") or ""
moved = before != after
proposed = row.get("edits_proposed", 0)
accepted = row.get("edits_accepted", 0)
br_before = row.get("beat_rate_before")
br_after = row.get("beat_rate_after")


def rate(value):
    return "-" if value is None else f"{float(value):.2f}"


if row.get("reason", "").startswith("guard"):
    head = "craft-train: skipped"
    body = row.get("reason", "")
elif accepted:
    head = f"craft-train: kept {accepted} of {proposed} edit(s)"
    body = f"beat rate {rate(br_before)} -> {rate(br_after)}"
else:
    head = f"craft-train: rejected all {proposed} edit(s)"
    # Do not assert the file is unchanged; say what the hashes actually show.
    # Printing "unchanged" next to a warning that it changed is how a broken
    # gate gets read as a working one.
    state = "craft file unchanged" if not moved else "craft file MOVED"
    body = f"beat rate {rate(br_before)} -> {rate(br_after)}, {state}"

# The file moving without an accepted edit, or staying put after one, are both
# contradictions worth shouting about rather than quietly formatting away.
if moved and not accepted:
    body += " | WARNING: craft file changed with no accepted edit"
if accepted and not moved:
    body += " | WARNING: edit accepted but craft file did not change"

print(f"{head}\n{body}\n{row.get('ts', '')}  sha {before[:8]} -> {after[:8]}")
PYEOF
)"

[ -n "$MESSAGE" ] || { echo "craft-train-notify: empty ledger"; exit 0; }
printf '%s\n' "$MESSAGE"

SENDER="$REPO_ROOT/skills/_shared/send-telegram.sh"
if [ -n "$TARGET" ]; then
  "$SENDER" "$MESSAGE" "$TARGET" >/dev/null 2>&1 \
    || echo "craft-train-notify: telegram send failed (report only, not fatal)" >&2
else
  "$SENDER" "$MESSAGE" >/dev/null 2>&1 \
    || echo "craft-train-notify: telegram send failed (report only, not fatal)" >&2
fi
exit 0
