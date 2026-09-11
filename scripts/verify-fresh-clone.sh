#!/usr/bin/env bash
# Prove the canonical branch works from a shallow fresh clone with no sibling checkout.
set -euo pipefail

SOURCE_REPO="${LIFE_MANAGER_VERIFY_REPO:-https://github.com/Daisuke134/life-manager.git}"
REF="${LIFE_MANAGER_VERIFY_REF:-main}"
VERIFY_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/life-manager-fresh-clone.XXXXXX")"
CLONE="$VERIFY_ROOT/checkout"
RUNTIME="$VERIFY_ROOT/runtime"
LOG="$VERIFY_ROOT/verify.log"

cleanup() {
  if [ "${LIFE_MANAGER_KEEP_VERIFY_DIR:-0}" = "1" ]; then
    printf 'fresh-clone directory retained: %s\n' "$VERIFY_ROOT"
  else
    chmod -R u+w "$VERIFY_ROOT" 2>/dev/null || true
    rm -rf "$VERIFY_ROOT"
  fi
}
trap cleanup EXIT

run() {
  printf '+ %s\n' "$*" | tee -a "$LOG"
  "$@" >>"$LOG" 2>&1
}

git clone --depth 1 --branch "$REF" --single-branch "$SOURCE_REPO" "$CLONE" >>"$LOG" 2>&1
COMMIT="$(git -C "$CLONE" rev-parse HEAD)"
BEFORE="$(git -C "$CLONE" status --porcelain --untracked-files=all)"
[ -z "$BEFORE" ] || { printf 'fresh clone is dirty before verification\n' >&2; exit 1; }

unset ANICCA_HOME LIFE_MANAGER_REPO PROFITABLE_CLAUDE_ROOT OPENCLAW_HOME
export HOME="$VERIFY_ROOT/home"
export npm_config_cache="$VERIFY_ROOT/npm-cache"
mkdir -p "$HOME"

run npm --prefix "$CLONE" run verify:oss
run env \
  LIFE_MANAGER_HOME="$RUNTIME" \
  LIFE_MANAGER_INSTALL_DAEMON=0 \
  bash "$CLONE/install.sh"
run npm --prefix "$CLONE/apps/life-manager" test
run npm --prefix "$CLONE/apps/life-manager" run eval
run npm --prefix "$CLONE/apps/life-manager" run eval:panel-privacy

AFTER="$(git -C "$CLONE" status --porcelain --untracked-files=all)"
[ -z "$AFTER" ] || {
  printf 'fresh clone became dirty during verification:\n%s\n' "$AFTER" >&2
  exit 1
}
[ -f "$RUNTIME/.env" ] || { printf 'installer did not create runtime .env\n' >&2; exit 1; }
[ ! -e "$HOME/Library/LaunchAgents" ] || {
  printf 'daemon-free installer changed LaunchAgents\n' >&2
  exit 1
}

APP_TESTS="$(awk '$1 == "ℹ" && $2 == "tests" && $3 > max { max = $3 } END { print max }' "$LOG")"
APP_PASS="$(awk '$1 == "ℹ" && $2 == "pass" && $3 > max { max = $3 } END { print max }' "$LOG")"
PANEL_TESTS="$(grep -E 'ℹ tests [0-9]+' "$LOG" | tail -1 | awk '{print $3}')"
printf 'FRESH_CLONE_PASS commit=%s app_tests=%s app_pass=%s panel_tests=%s runtime=%s\n' \
  "$COMMIT" "${APP_TESTS:-unknown}" "${APP_PASS:-unknown}" "${PANEL_TESTS:-unknown}" "$RUNTIME"
