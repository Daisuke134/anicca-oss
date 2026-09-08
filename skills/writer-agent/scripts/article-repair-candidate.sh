#!/usr/bin/env bash
# SSOT §9.3.1 item H2, wired. One bounded repair attempt per run, for the one
# incident a completed investigation has already handed to this stage.
#
# This worker deliberately does NOT do three things, and each omission is what
# makes the placement safe:
#
#   1. it never takes the lock the daily creator and the resume tick share, so
#      a 900-second repair cannot delay or starve a 300-second recovery tick,
#      the zenn retry worker, or the 06:00 creator;
#   2. it never creates a run and never performs a shipment, so R6's "exactly
#      one daily creator, exactly one same-run recovery owner" still holds --
#      this label is neither;
#   3. it never loads the runtime credential file, so no destination secret is
#      even present in this process, let alone in the model child.
#
# Deploying the candidate, resuming the work item and the public readback are
# Order 5 and are not performed here. This script stops at a registered
# verified candidate.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"

ARTICLE_ROOT="${ARTICLE_ROOT:-${ARTICLE_SKILL_DIR:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}}"
# shellcheck source=writer-runtime-env.sh
source "$ARTICLE_ROOT/scripts/writer-runtime-env.sh" || exit $?
STATE_DIR="$WRITER_STATE_DIR"
LOG="${ARTICLE_REPAIR_LOG:-$WRITER_LOG_DIR/article-repair-candidate.log}"
MODEL_RUNNER="${ARTICLE_MODEL_RUNNER:-$ARTICLE_ROOT/runtime/model-runner.sh}"
BASE_REF="${ARTICLE_REPAIR_BASE_REF:-HEAD}"
# Candidate worktrees are regenerable Writer runtime state, outside the source
# checkout but inside the one shared local/cloud state contract.
REPAIR_ROOT="${ARTICLE_REPAIR_ROOT:-$WRITER_STATE_DIR/self-heal/repair-candidates}"

mkdir -p "$(dirname "$LOG")"

QUEUE="$STATE_DIR/self-heal/incident-queue.json"
[ -f "$QUEUE" ] || exit 0

REPO="${ARTICLE_REPAIR_REPO:-${LIFE_MANAGER_SOURCE_REPO:-}}"
if [ -z "$REPO" ] || ! git -C "$REPO" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "article-repair-candidate: LIFE_MANAGER_SOURCE_REPO is not a git checkout" >>"$LOG"
  exit 0
fi

python3 "$ARTICLE_ROOT/scripts/writer_repair_candidate_dispatch.py" \
  --state-root "$STATE_DIR" \
  --repo "$REPO" \
  --base-ref "$BASE_REF" \
  --repair-root "$REPAIR_ROOT" \
  --model-runner "$MODEL_RUNNER" \
  --observed-at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  >>"$LOG" 2>&1 \
  || echo "article-repair-candidate: repair channel failed closed" >>"$LOG"

exit 0
