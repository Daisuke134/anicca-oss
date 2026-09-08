#!/bin/bash
set -u

canonical_home() {
  /usr/bin/python3 -I -c \
    'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)'
}

if ! canonical_home="$(canonical_home 2>/dev/null)"; then
  printf '%s\n' 'lm-recording-store: canonical home lookup failed' >&2
  exit 1
fi
if [[ "$canonical_home" != /* || ! -d "$canonical_home" ]]; then
  printf '%s\n' 'lm-recording-store: canonical home is invalid' >&2
  exit 1
fi

disk_guard="$canonical_home/gig/releases/life-manager/current/runtime/host/disk_admission.py"
if [[ -L "$disk_guard" || ! -f "$disk_guard" || ! -r "$disk_guard" ]]; then
  printf '%s\n' "lm-recording-store: disk guard is missing or unsafe: $disk_guard" >&2
  exit 1
fi

export HOME="$canonical_home"
export LIFE_MANAGER_DISK_HEADROOM_KIB=524288
export LIFE_MANAGER_HOST_STATE_DIR="$canonical_home/.local/state/life-manager/state"
export LIFE_MANAGER_PRODUCER_STATE_DIR="$canonical_home/.local/state/life-manager/lm-recording-store"
unset BASH_ENV ENV PS4
unset LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP
unset GIG_DISK_HEADROOM_KIB GIG_HOST_STATE_DIR GIG_STATE_DIR
unset GIG_IGNORE_DISK_PRESSURE_BLOCK GIG_IGNORE_DISK_WRITERS_STOP
unset DISK_CONTROL_STATE_DIR OPENCLAW_STATE_DIR

/usr/bin/python3 -I "$disk_guard" /usr/bin/true || exit 1

exec "$HOME/.openclaw/skills/life-manager-video/run-store-recordings.sh"
