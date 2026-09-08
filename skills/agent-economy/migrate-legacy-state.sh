#!/usr/bin/env bash
set -euo pipefail
umask 077

INSTANCE_HOME="${AGENT_ECONOMY_LEGACY_INSTANCE_HOME:-${ANICCA_HOME:-$HOME/.anicca}}"
OWNER_HOME="${AGENT_ECONOMY_LEGACY_OWNER_HOME:-$HOME}"
TARGET="${AGENT_ECONOMY_STATE_ROOT:-${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager}/agent-economy}"
INSTANCE_TARGET="$TARGET/instance"
OLD_EARN="$INSTANCE_HOME/skills/earn/state"

fail_destination() {
  printf 'unsafe Agent Economy migration destination: %s\n' "$1" >&2
  exit 2
}

reject_symlink_components() {
  local component="$1"
  while [ "$component" != "/" ]; do
    [ ! -L "$component" ] || fail_destination "path contains a symlink"
    component="$(dirname "$component")"
  done
}

validate_paths() {
  local source
  [ -n "$TARGET" ] || fail_destination "empty path"
  case "$TARGET" in /*) ;; *) fail_destination "path must be absolute" ;; esac
  case "$TARGET/" in *"/../"*|*"/./"*|*"//"*) fail_destination "path contains an unsafe component" ;; esac
  TARGET="${TARGET%/}"
  case "$TARGET" in
    /|"$HOME"|"$HOME/.local"|"$HOME/.local/state"|"$HOME/.local/state/life-manager")
      fail_destination "path is too broad"
      ;;
  esac
  for source in "$OLD_EARN" "$INSTANCE_HOME/.blockrun" "$OWNER_HOME/.hermes/state"; do
    case "$source" in /*) ;; *) fail_destination "source path must be absolute" ;; esac
    case "$source/" in *"/../"*|*"/./"*|*"//"*) fail_destination "source path contains an unsafe component" ;; esac
    source="${source%/}"
    reject_symlink_components "$source"
    case "$TARGET/" in "$source/"*) fail_destination "path overlaps a source" ;; esac
    case "$source/" in "$TARGET/"*) fail_destination "path contains a source" ;; esac
  done
  reject_symlink_components "$TARGET"
  [ ! -e "$TARGET" ] || [ -d "$TARGET" ] || fail_destination "existing path is not a directory"
  if [ -d "$TARGET" ] && find "$TARGET" -type l -print -quit | grep -q .; then
    fail_destination "existing tree contains a symlink"
  fi
}

validate_paths
mkdir -p "$TARGET"
chmod 700 "$TARGET"

copy_once() {
  local source="$1" target="$2" temporary
  [ -f "$source" ] || return 0
  reject_symlink_components "$source"
  mkdir -p "$(dirname "$target")"
  chmod 700 "$(dirname "$target")"
  if [ ! -e "$target" ]; then
    temporary="$(mktemp "$TARGET/.migration.XXXXXX")"
    if ! cp -p "$source" "$temporary"; then
      rm -f -- "$temporary"
      return 1
    fi
    if ! cmp -s "$source" "$temporary"; then
      rm -f -- "$temporary"
      printf 'Agent Economy migration copy verification failed\n' >&2
      return 1
    fi
    chmod 600 "$temporary"
    if ln "$temporary" "$target" 2>/dev/null; then
      rm -f -- "$temporary"
    elif [ -f "$target" ] && [ ! -L "$target" ]; then
      rm -f -- "$temporary"
    else
      rm -f -- "$temporary"
      fail_destination "file publish failed"
    fi
  fi
  [ -f "$target" ] && [ ! -L "$target" ] || fail_destination "existing target is not a regular file"
  chmod 600 "$target"
}

for name in earn-ledger.jsonl receipt-reconciliations.jsonl revenue-receipts.inbox.jsonl revenue-receipts.jsonl; do
  copy_once "$OLD_EARN/$name" "$TARGET/$name"
done
copy_once "$INSTANCE_HOME/.blockrun/compute-receipts.jsonl" "$TARGET/compute-receipts.jsonl"
copy_once "$OWNER_HOME/.hermes/state/shelter-cost.jsonl" "$TARGET/shelter-cost.jsonl"

copy_once "$INSTANCE_HOME/.automaton/wallet.json" "$INSTANCE_TARGET/.automaton/wallet.json"
copy_once "$INSTANCE_HOME/.env" "$INSTANCE_TARGET/.env"
copy_once "$INSTANCE_HOME/identity/genesis.md" "$INSTANCE_TARGET/identity/genesis.md"
copy_once "$INSTANCE_HOME/identity/name" "$INSTANCE_TARGET/identity/name"
copy_once "$INSTANCE_HOME/state/ledger.jsonl" "$INSTANCE_TARGET/state/ledger.jsonl"
copy_once "$INSTANCE_HOME/state/harness-failures.jsonl" \
  "$INSTANCE_TARGET/state/harness-failures.jsonl"
copy_once "$OLD_EARN/earn-ledger.jsonl" \
  "$INSTANCE_TARGET/state/skills/earn/earn-ledger.jsonl"

echo "Agent Economy legacy state copied to $TARGET; source retained."
