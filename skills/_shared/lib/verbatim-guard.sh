#!/usr/bin/env bash
# verbatim-guard.sh — block posts containing known borrowed phrases from named creators.
# Spec: ANICCA_USEFUL_CONTENT_SPEC.md HR-J
#
# Usage:
#   . verbatim-guard.sh
#   vg_check "text body"     # exit 0 = clean, exit 1 = blocked
# CLI:
#   bash verbatim-guard.sh /path/to/file
#   echo "text" | bash verbatim-guard.sh

VG_BLACKLIST="${WRITER_CONTENT_LIBRARY_DIR:-${WRITER_STATE_DIR:?WRITER_STATE_DIR is required}/content-library}/verbatim_blacklist.txt"

# vg_check <text>
#   exit 0 = clean
#   exit 1 = contains blacklisted phrase (prints offending phrase to stderr)
vg_check() {
  local text="$1"
  [[ -f "$VG_BLACKLIST" ]] || return 0  # no blacklist = pass (degrade gracefully)
  while IFS= read -r phrase; do
    # skip comments and blank lines
    [[ -z "$phrase" || "$phrase" =~ ^# ]] && continue
    if printf '%s' "$text" | grep -F -- "$phrase" >/dev/null 2>&1; then
      echo "verbatim-guard: blocked phrase '$phrase'" >&2
      return 1
    fi
  done < "$VG_BLACKLIST"
  return 0
}

# CLI fallback
if [[ "${BASH_SOURCE[0]:-}" == "${0}" ]]; then
  if [[ -n "${1:-}" && -f "$1" ]]; then
    vg_check "$(cat "$1")"
  else
    vg_check "$(cat /dev/stdin)"
  fi
fi
