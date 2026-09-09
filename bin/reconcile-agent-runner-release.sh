#!/usr/bin/env bash
# Keep managed runners on one complete pushed-main release without interrupting active loops.
set -euo pipefail

SOURCE_REPO="${LIFE_MANAGER_SOURCE_REPO:-$HOME/Projects/life-manager-main}"
LOOPS_ROOT="${LOOPS_ROOT:-$HOME/loops}"
CURRENT="$LOOPS_ROOT/current"

git -C "$SOURCE_REPO" fetch --quiet origin main
main_sha="$(git -C "$SOURCE_REPO" rev-parse origin/main)"
current_sha="$(jq -r '.sha // ""' "$CURRENT/RELEASE.json" 2>/dev/null || true)"
current_paths="$(jq -r '.release_paths // ""' "$CURRENT/RELEASE.json" 2>/dev/null || true)"
current_complete=0
[ "$current_paths" = "ALL" ] && current_complete=1
release_sha_target="$main_sha"

# The public specs and the Gig progress ledger are not runtime inputs. Re-exporting the complete
# tree for a progress-only main commit consumes about a GiB while changing no runnable byte.
# Keep reconciling stale target labels to the existing complete release; cut a new release as soon
# as any other path differs. A missing/non-ancestor current SHA fails closed into a cut.
if [ "$current_complete" -eq 1 ] && [ -n "$current_sha" ] \
  && git -C "$SOURCE_REPO" merge-base --is-ancestor "$current_sha" "$main_sha" 2>/dev/null \
  && git -C "$SOURCE_REPO" diff --quiet "$current_sha" "$main_sha" -- . \
    ':(exclude)docs/**' ':(exclude)skills/earn/gig/TODO.md'; then
  release_sha_target="$current_sha"
fi

if [ "$release_sha_target" != "$current_sha" ] || [ "$current_complete" -ne 1 ]; then
  cutter="$CURRENT/bin/cut-loop-release.sh"
  [ -x "$cutter" ] || cutter="$SOURCE_REPO/bin/cut-loop-release.sh"
  LIFE_MANAGER_SOURCE_REPO="$SOURCE_REPO" LOOPS_ROOT="$LOOPS_ROOT" LOOPS_RELEASE_PATHS= \
    bash "$cutter" "$main_sha"
fi

RELEASE_ROOT="$(cd "$CURRENT" && pwd -P)"
release_sha="$(jq -r '.sha // ""' "$RELEASE_ROOT/RELEASE.json")"
release_paths="$(jq -r '.release_paths // ""' "$RELEASE_ROOT/RELEASE.json")"
if [ "$release_sha" != "$release_sha_target" ] || [ "$release_paths" != "ALL" ]; then
  printf 'agent-runner reconcile refused: release is not the full pushed-main build\n' >&2
  exit 1
fi

status=0
if ! LIFE_MANAGER_RELEASE_ROOT="$RELEASE_ROOT" "$RELEASE_ROOT/bin/lm-loop" reconcile shared-agent-runner --loaded-idle-only --loop-id hf-gig-apply-direct; then
  status=1
fi
if ! LIFE_MANAGER_RELEASE_ROOT="$RELEASE_ROOT" "$RELEASE_ROOT/bin/lm-loop" reconcile shared-agent-runner --include-running --loop-id hf-gig-reply-detector; then
  status=1
fi
if ! LIFE_MANAGER_RELEASE_ROOT="$RELEASE_ROOT" "$RELEASE_ROOT/bin/lm-loop" reconcile deterministic --loaded-idle-only --loop-id hf-gig-storefront-direct --loop-id hf-gig-paid-direct --loop-id life-manager-disk-cleanup; then
  status=1
fi
exit "$status"
