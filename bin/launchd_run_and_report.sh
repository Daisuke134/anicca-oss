#!/usr/bin/env bash
set -uo pipefail

label=""
channel=""
target=""

usage() {
  printf 'usage: %s --label LABEL --channel CHANNEL --target TARGET -- COMMAND [ARG ...]\n' "$0" >&2
}

while (($# > 0)); do
  case "$1" in
    --label)
      (($# >= 2)) || { usage; exit 2; }
      label="$2"
      shift 2
      ;;
    --channel)
      (($# >= 2)) || { usage; exit 2; }
      channel="$2"
      shift 2
      ;;
    --target)
      (($# >= 2)) || { usage; exit 2; }
      target="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$label" || -z "$channel" || -z "$target" || $# -eq 0 ]]; then
  usage
  exit 2
fi

canonical_home() {
  /usr/bin/python3 -I -c \
    'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)'
}

case "$label" in
  reelclaw-*|larry-*-reel-*|larry-anicca-*|larry-*-library-post|watercolor-jp-*)
    if ! canonical_home="$(canonical_home 2>/dev/null)"; then
      printf '%s canonical home lookup failed\n' "$label" >&2
      exit 1
    fi
    if [[ "$canonical_home" != /* || ! -d "$canonical_home" ]]; then
      exit 1
    fi
    disk_guard="$canonical_home/gig/releases/life-manager/current/runtime/host/disk_admission.py"
    if [[ -L "$disk_guard" || ! -f "$disk_guard" || ! -r "$disk_guard" ]]; then
      exit 1
    fi
    export HOME="$canonical_home"
    export LIFE_MANAGER_DISK_HEADROOM_KIB=524288
    export LIFE_MANAGER_HOST_STATE_DIR="$canonical_home/.local/state/life-manager/state"
    export LIFE_MANAGER_PRODUCER_STATE_DIR="$canonical_home/.local/state/life-manager/reelclaw-media"
    unset LIFE_MANAGER_IGNORE_DISK_PRESSURE_BLOCK LIFE_MANAGER_IGNORE_DISK_WRITERS_STOP
    unset GIG_DISK_HEADROOM_KIB GIG_HOST_STATE_DIR GIG_STATE_DIR
    unset GIG_IGNORE_DISK_PRESSURE_BLOCK GIG_IGNORE_DISK_WRITERS_STOP
    unset DISK_CONTROL_STATE_DIR OPENCLAW_STATE_DIR
    /usr/bin/python3 -I "$disk_guard" /usr/bin/true || exit 1
    ;;
esac

report_max_bytes="${LAUNCHD_REPORT_MAX_BYTES:-3000}"
if ! [[ "$report_max_bytes" =~ ^[1-9][0-9]*$ ]]; then
  printf 'LAUNCHD_REPORT_MAX_BYTES must be a positive integer\n' >&2
  exit 2
fi

stdout_file="$(mktemp -t launchd-run-report-out.XXXXXX)"
stderr_file="$(mktemp -t launchd-run-report-err.XXXXXX)"
cleanup() {
  rm -f "$stdout_file" "$stderr_file"
}
trap cleanup EXIT

"$@" >"$stdout_file" 2>"$stderr_file"
command_status=$?

cat "$stdout_file"
cat "$stderr_file" >&2

stream_budget=$((report_max_bytes / 2))
if ((stream_budget < 1)); then
  stream_budget=1
fi
stdout_tail="$(tail -c "$stream_budget" "$stdout_file")"
stderr_tail="$(tail -c "$stream_budget" "$stderr_file")"
report_message="${label} exit=${command_status}"
if [[ -n "$stdout_tail" ]]; then
  report_message+=$'\nstdout:\n'"$stdout_tail"
fi
if [[ -n "$stderr_tail" ]]; then
  report_message+=$'\nstderr:\n'"$stderr_tail"
fi
if [[ -z "$stdout_tail" && -z "$stderr_tail" ]]; then
  report_message+=$'\n(no output)'
fi

openclaw_bin="${LAUNCHD_REPORT_OPENCLAW_BIN:-/opt/homebrew/bin/openclaw}"
"$openclaw_bin" message send \
  --channel "$channel" \
  --target "$target" \
  --message "$report_message" \
  --json >/dev/null 2>&1 || true

exit "$command_status"
