#!/bin/zsh
set -euo pipefail

if ! CANONICAL_HOME="$(/usr/bin/python3 -I - <<'PY'
import os
import pwd
import sys

try:
    home = pwd.getpwuid(os.getuid()).pw_dir
except (KeyError, OSError):
    raise SystemExit(1)
if not isinstance(home, str) or not os.path.isabs(home) or not os.path.isdir(home):
    raise SystemExit(1)
sys.stdout.write(home)
PY
)"; then
  print -u2 "job-search browser: canonical home is unavailable"
  exit 1
fi
export HOME="$CANONICAL_HOME"

SCRIPT_DIR="${0:A:h}"
DISK_GUARD="${SCRIPT_DIR:h:h:h}/runtime/host/disk_admission.py"
PORT_OWNER="${JOB_SEARCH_BROWSER_PORT_OWNER-${SCRIPT_DIR:h:h:h}/runtime/host/browser_port_owner.py}"
if [[ ! -f "$DISK_GUARD" || -L "$DISK_GUARD" || ! -r "$DISK_GUARD" ]]; then
  print -u2 "job-search browser: disk guard is missing or unsafe"
  exit 1
fi

unset GIG_IGNORE_DISK_PRESSURE_BLOCK \
  GIG_IGNORE_DISK_WRITERS_STOP \
  LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK \
  LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP \
  GIG_DISK_HEADROOM_KIB \
  GIG_HOST_STATE_DIR \
  GIG_STATE_DIR \
  DISK_CONTROL_STATE_DIR \
  OPENCLAW_STATE_DIR
if [[ "${LIFE_MANAGER_LOOP_ID:-}" == "job-search-mercor-browser" ]]; then
  if [[ -n "${JOB_SEARCH_BROWSER_STATE_NAME+x}" && "$JOB_SEARCH_BROWSER_STATE_NAME" != "mercor-browser" ]]; then
    print -u2 "job-search browser: Mercor loop requires mercor-browser state"
    exit 2
  fi
  BROWSER_STATE_NAME="mercor-browser"
elif [[ -n "${JOB_SEARCH_BROWSER_STATE_NAME+x}" ]]; then
  BROWSER_STATE_NAME="$JOB_SEARCH_BROWSER_STATE_NAME"
else
  BROWSER_STATE_NAME="job-search-browser"
fi
if [[ ! "$BROWSER_STATE_NAME" =~ '^[A-Za-z0-9][A-Za-z0-9._-]*$' ]]; then
  print -u2 "job-search browser: invalid browser state name"
  exit 2
fi
if [[ "$BROWSER_STATE_NAME" == "mercor-browser" ]]; then
  export LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK=1
fi
LIFE_MANAGER_DISK_HEADROOM_KIB=524288
LIFE_MANAGER_HOST_STATE_DIR="$CANONICAL_HOME/.local/state/life-manager/state"
if [[ "$BROWSER_STATE_NAME" == "job-search-browser" ]]; then
  LIFE_MANAGER_PRODUCER_STATE_DIR="$CANONICAL_HOME/.local/state/life-manager/job-search-browser"
else
  LIFE_MANAGER_PRODUCER_STATE_DIR="$CANONICAL_HOME/.local/state/life-manager/$BROWSER_STATE_NAME"
fi
export LIFE_MANAGER_DISK_HEADROOM_KIB LIFE_MANAGER_HOST_STATE_DIR LIFE_MANAGER_PRODUCER_STATE_DIR

if ! /usr/bin/python3 -I "$DISK_GUARD" /usr/bin/true; then
  print -u2 "job-search browser: disk guard blocked browser start"
  exit 1
fi

if [[ "$BROWSER_STATE_NAME" == "mercor-browser" ]]; then
  BROWSER_PORT="${JOB_SEARCH_BROWSER_PORT-9334}"
  BROWSER_FINGERPRINT="${JOB_SEARCH_BROWSER_FINGERPRINT-81234}"
else
  BROWSER_PORT="${JOB_SEARCH_BROWSER_PORT-9222}"
  BROWSER_FINGERPRINT="${JOB_SEARCH_BROWSER_FINGERPRINT-80137}"
fi
if [[ ! "$BROWSER_PORT" =~ '^[0-9]+$' ]]; then
  print -u2 "job-search browser: invalid browser port"
  exit 2
fi
if (( 10#$BROWSER_PORT < 1 || 10#$BROWSER_PORT > 65535 )); then
  print -u2 "job-search browser: invalid browser port"
  exit 2
fi
if [[ ! "$BROWSER_FINGERPRINT" =~ '^[0-9]+$' ]]; then
  print -u2 "job-search browser: invalid browser fingerprint"
  exit 2
fi
if [[ "$BROWSER_STATE_NAME" == "mercor-browser" && -z "${JOB_SEARCH_BROWSER_PROFILE+x}" ]]; then
  PROFILE="$(
    /usr/bin/python3 -I - "$HOME/.local/state/anicca/job-search/mercor/resume-state.json" <<'PY'
import json, os, sys

try:
    profile = json.load(open(sys.argv[1], encoding="utf-8"))["browser"]["profile"]
except (KeyError, OSError, TypeError, UnicodeError, ValueError):
    raise SystemExit(1)
if not isinstance(profile, str) or not profile or not os.path.isabs(profile):
    raise SystemExit(1)
print(profile, end="")
PY
  )" || PROFILE=""
  if [[ -z "$PROFILE" ]]; then
    PROFILE="$HOME/.cloak/profiles/job-search-mercor"
  fi
else
  PROFILE="${JOB_SEARCH_BROWSER_PROFILE-$HOME/.cloak/profiles/job-search-daily}"
fi
if [[ "$PROFILE" != /* ]]; then
  print -u2 "job-search browser: browser profile must be absolute"
  exit 2
fi
if [[ "$BROWSER_STATE_NAME" == "mercor-browser" ]]; then
  if (( 10#$BROWSER_PORT == 9222 )); then
    print -u2 "job-search browser: Mercor browser port cannot be 9222"
    exit 2
  fi
  DAILY_PROFILE="$HOME/.cloak/profiles/job-search-daily"
  if [[ "${PROFILE:A}" == "${DAILY_PROFILE:A}" ]]; then
    print -u2 "job-search browser: Mercor browser profile cannot be the daily browser profile"
    exit 2
  fi
fi
if [[ ! -f "$PORT_OWNER" || -L "$PORT_OWNER" || ! -r "$PORT_OWNER" ]]; then
  print -u2 "job-search browser: port owner is missing or unsafe"
  exit 1
fi
PORT_OWNER_STATE_ARGUMENT=()
if [[ -n "${JOB_SEARCH_BROWSER_PORT_STATE_DIR:-}" ]]; then
  if [[ "$JOB_SEARCH_BROWSER_PORT_STATE_DIR" != /* ]]; then
    print -u2 "job-search browser: port state dir must be absolute"
    exit 2
  fi
  PORT_OWNER_STATE_ARGUMENT=(--state-dir "$JOB_SEARCH_BROWSER_PORT_STATE_DIR")
fi
if ! /usr/bin/pgrep -f -- "--user-data-dir=$PROFILE" >/dev/null 2>&1; then
  /bin/rm -f "$PROFILE/SingletonLock" "$PROFILE/SingletonSocket" "$PROFILE/SingletonCookie" 2>/dev/null || true
fi
mkdir -p "$PROFILE"
chmod 700 "$PROFILE"

if [[ "$BROWSER_FINGERPRINT" == "80137" ]]; then
  FINGERPRINT_ARGUMENT=(--fingerprint=80137)
else
  FINGERPRINT_ARGUMENT=(--fingerprint="$BROWSER_FINGERPRINT")
fi
if [[ "$BROWSER_PORT" == "9222" ]]; then
  PORT_ARGUMENT=(--remote-debugging-port=9222)
else
  PORT_ARGUMENT=(--remote-debugging-port="$BROWSER_PORT")
fi

CHROMIUM_BIN=$(ls -d "$HOME"/.cloakbrowser/chromium-*/Chromium.app/Contents/MacOS/Chromium(N) | sort -V | tail -1)
[[ -x "$CHROMIUM_BIN" ]] || {
  print -u2 "job-search browser: CloakBrowser Chromium is missing"
  exit 69
}

exec /usr/bin/python3 -I "$PORT_OWNER" run \
  "${PORT_OWNER_STATE_ARGUMENT[@]}" \
  --port "$BROWSER_PORT" \
  --profile "$PROFILE" \
  --owner "$BROWSER_STATE_NAME" \
  -- "$CHROMIUM_BIN" \
  --no-first-run \
  --no-default-browser-check \
  --password-store=basic \
  --use-mock-keychain \
  --disable-sync \
  --disable-features=MacAppCodeSignClone \
  --no-sandbox \
  "${FINGERPRINT_ARGUMENT[@]}" \
  --fingerprint-platform=macos \
  --remote-debugging-address=127.0.0.1 \
  --remote-allow-origins='*' \
  "${PORT_ARGUMENT[@]}" \
  --user-data-dir="$PROFILE" \
  about:blank
