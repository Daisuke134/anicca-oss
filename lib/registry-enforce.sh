#!/usr/bin/env bash
# lib/registry-enforce.sh — REQ-CEO-009/REQ-CEO-014: the registry enforcement gate every live loop
# CLI sources before spawning its tmux/claude core. Provides:
#
#   registry_enforce_or_exit <loop>
#     (0) invokes bin/budget-check.sh --loop <loop> (dispatch-time hard-budget-breach check,
#         REQ-CEO-009 step 0) BEFORE any start decision;
#     (a) `exit 1` (the calling script itself exits -- this function is meant to be sourced and
#         called directly, never via command substitution) if allocation.status == "paused";
#     (b) computes + exports EFFECTIVE_INTERVAL_SECONDS / EFFECTIVE_CRON and writes
#         state/effective-cron/<loop>.txt (REQ-CEO-014) for normal/reduce/double_down;
#     (c) fails OPEN (returns 0, logs a stderr warning, never blocks the start) on a missing/corrupt
#         registry, an unknown loop name, a missing/failing bin/budget-check.sh, or a missing/
#         non-positive base_interval_seconds (cadence computation skipped only, start unaffected).
#
#   compute_effective_cron_from_interval_seconds <seconds> [base_minute] [base_hour]
#     Pure function (no side effects): prints the derived cron expression for the given effective
#     interval, banding per REQ-CEO-014 (see inline comments below for the 3 bands + clamps).
#
# The repository registry is immutable configuration. CEO_STATE_DIR overrides only mutable CEO
# ledgers/allocation/cadence state; production defaults to one central Life Manager state root.
REGISTRY_ENFORCE_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY_ENFORCE_PY=/opt/homebrew/bin/python3
[ -x "$REGISTRY_ENFORCE_PY" ] || REGISTRY_ENFORCE_PY=python3

_registry_enforce_base() {
  if [ -n "${LIFE_MANAGER_RELEASE_SHA:-}" ]; then
    echo "${LIFE_MANAGER_CEO_STATE_ROOT:-$HOME/.local/state/life-manager/ceo-runner}"
  else
    echo "${CEO_STATE_DIR:-${LIFE_MANAGER_CEO_STATE_ROOT:-$HOME/.local/state/life-manager/ceo-runner}}"
  fi
}

_registry_enforce_config_root() {
  if [ -n "${LIFE_MANAGER_RELEASE_SHA:-}" ]; then
    echo "$REGISTRY_ENFORCE_REPO_ROOT"
  else
    echo "${CEO_CONFIG_ROOT:-$REGISTRY_ENFORCE_REPO_ROOT}"
  fi
}

# compute_effective_cron_from_interval_seconds — REQ-CEO-014's 3 non-overlapping bands:
#   (i)   < 3600s   -> "*/<minutes> * * * *"      (values <60s clamp to the 60s floor)
#   (ii)  < 86400s  -> "0 */<hours> * * *"        (fixed minute 0, nearest-hour rounding)
#   (iii) >= 86400s -> "<base_minute> <base_hour> */<day_divisor> * *" (day divisor clamped <=28)
compute_effective_cron_from_interval_seconds() {
  local seconds="${1:-0}" base_minute="${2:-0}" base_hour="${3:-0}"
  "$REGISTRY_ENFORCE_PY" - "$seconds" "$base_minute" "$base_hour" <<'PYEOF'
import sys


def to_int(value, default=0):
    try:
        return int(round(float(value)))
    except Exception:
        return default


seconds = to_int(sys.argv[1], 0)
base_minute = to_int(sys.argv[2], 0)
base_hour = to_int(sys.argv[3], 0)

if seconds < 60:
    sys.stderr.write(f"registry-enforce: NOTICE effective interval {seconds}s clamped to the 60s floor\n")
    seconds = 60

if seconds < 3600:
    minutes = max(1, to_int(seconds / 60, 1))
    print(f"*/{minutes} * * * *")
elif seconds < 86400:
    hours = max(1, to_int(seconds / 3600, 1))
    print(f"0 */{hours} * * *")
else:
    divisor = to_int(seconds / 86400, 1)
    if divisor > 28:
        sys.stderr.write(f"registry-enforce: NOTICE day divisor {divisor} clamped to 28 (cron day-of-month max)\n")
        divisor = 28
    divisor = max(1, divisor)
    print(f"{base_minute} {base_hour} */{divisor} * *")
PYEOF
}

registry_enforce_or_exit() {
  local loop="${1:-}"
  local base config_root registry overrides budget_script
  base="$(_registry_enforce_base)"
  config_root="$(_registry_enforce_config_root)"
  registry="$config_root/config/loop-registry.json"
  overrides="$base/config/allocation-overrides.json"
  budget_script="$REGISTRY_ENFORCE_REPO_ROOT/bin/budget-check.sh"

  # step 0: dispatch-time hard-budget check, BEFORE any start decision (REQ-CEO-009 step 0).
  # Fails open on its own: a missing/failing budget-check.sh never blocks the start decision below.
  if [ -x "$budget_script" ]; then
    if ! CEO_STATE_DIR="$base" CEO_CONFIG_ROOT="$config_root" bash "$budget_script" --loop "$loop" >/dev/null 2>&1; then
      echo "registry-enforce: WARNING bin/budget-check.sh exited non-zero for loop '$loop' -- proceeding fail-open on the budget check itself" >&2
    fi
  else
    echo "registry-enforce: WARNING bin/budget-check.sh not found/executable at $budget_script -- skipping dispatch-time budget check (fail-open)" >&2
  fi

  if [ ! -f "$registry" ]; then
    echo "registry-enforce: WARNING registry missing at $registry, allowing '$loop' to start (fail-open)" >&2
    return 0
  fi

  local py_out RESULT="" STATUS="" MULT="" DETAIL="" BASE_INTERVAL="" BASE_MINUTE="" BASE_HOUR=""
  py_out="$("$REGISTRY_ENFORCE_PY" "$REGISTRY_ENFORCE_REPO_ROOT/lib/registry_enforce_read.py" "$registry" "$loop" "$overrides" 2>/dev/null)"
  eval "$py_out"

  case "$RESULT" in
    missing)
      echo "registry-enforce: WARNING registry missing at $registry, allowing '$loop' to start (fail-open)" >&2
      return 0
      ;;
    corrupt)
      echo "registry-enforce: WARNING registry corrupt (${DETAIL:-parse error}), allowing '$loop' to start (fail-open)" >&2
      return 0
      ;;
    unknown_loop)
      echo "registry-enforce: WARNING loop '$loop' not found in registry, allowing it to start (fail-open, unmanaged loop)" >&2
      return 0
      ;;
    ok)
      : # fall through
      ;;
    *)
      echo "registry-enforce: WARNING unexpected registry read result for '$loop', allowing it to start (fail-open)" >&2
      return 0
      ;;
  esac

  if [ "$STATUS" = "paused" ]; then
    echo "registry-enforce: loop '$loop' is PAUSED by CEO decision -- refusing to start" >&2
    exit 1
  fi

  # cadence computation (REQ-CEO-014) -- runs for normal/reduce/double_down alike; fails open
  # (skips ONLY the cadence recomputation, never blocks the start) on a missing/non-positive
  # base_interval_seconds.
  local base_is_positive=1
  if [ -z "$BASE_INTERVAL" ]; then
    base_is_positive=0
  elif ! "$REGISTRY_ENFORCE_PY" -c "import sys; sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "$BASE_INTERVAL" 2>/dev/null; then
    base_is_positive=0
  fi

  if [ "$base_is_positive" -eq 1 ]; then
    local eis cron cron_dir
    eis="$("$REGISTRY_ENFORCE_PY" -c "import sys; print(round(float(sys.argv[1]) / float(sys.argv[2])))" "$BASE_INTERVAL" "${MULT:-1.0}" 2>/dev/null)"
    if [ -n "$eis" ]; then
      export EFFECTIVE_INTERVAL_SECONDS="$eis"
      cron="$(compute_effective_cron_from_interval_seconds "$eis" "${BASE_MINUTE:-0}" "${BASE_HOUR:-0}" 2>/dev/null)"
      export EFFECTIVE_CRON="$cron"
      cron_dir="$base/state/effective-cron"
      mkdir -p "$cron_dir"
      printf '%s\n' "$cron" > "$cron_dir/$loop.txt"
    else
      echo "registry-enforce: WARNING cadence computation failed for '$loop', skipping (fail-open, start not blocked)" >&2
    fi
  else
    echo "registry-enforce: WARNING base_interval_seconds missing/non-positive for '$loop', skipping cadence computation (fail-open, start not blocked)" >&2
  fi

  return 0
}
