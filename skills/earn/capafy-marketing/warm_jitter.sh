#!/usr/bin/env bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# warm_jitter.sh — human-like timing: sleep a random 0-3h before running the warmup, so the daily
# warmup does NOT fire at the exact same clock time every day. launchd fires this at a base time;
# the random sleep spreads the actual run across a window.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=account_state.sh
. "$SCRIPT_DIR/account_state.sh"
ACCOUNTS_FILE="$(capafy_ig_accounts_file)"
IG_HANDLE="$(resolve_capafy_ig_handle "$ACCOUNTS_FILE")"
if [ -z "$IG_HANDLE" ]; then
  echo "capafy warmup: no active account -> PROVISION on daily pass" >&2
  exit 0
fi
sleep $(( RANDOM % 10800 ))   # 0..10800s = 0..3h jitter
PYTHON_BIN="${CAPAFY_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3 2>/dev/null)}}"
[ -n "$PYTHON_BIN" ] || { echo "python3 is required" >&2; exit 2; }
exec "$PYTHON_BIN" "$LIFE_MANAGER_REPO/skills/earn/marketing-engine/warmer.py" "$ACCOUNTS_FILE"
