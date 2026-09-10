#!/usr/bin/env bash
# ceo-pass.sh — REQ-CEO-058/070 entrypoint: one CEO pass, called by founder-loop.sh immediately
# before "exit $RC" (REQ-CEO-070) so a CEO pass runs on EVERY founder-loop wake, whether or not
# record-earn.mjs succeeded that wake (RC may be non-zero — PROP-CEO-022). This script never
# influences founder-loop.sh's own exit code (INV-H6) — the caller always ignores this script's own
# rc and re-exits the original $RC unchanged.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Test seam mirrors founder-loop.sh's own FOUNDER_TEST/FOUNDER_DIR pattern (REQ-CEO-070 disproof
# tests relocate state so a test run never touches the real ~/.anicca-founder/state/).
if [ "${FOUNDER_TEST:-}" = "1" ] && [ -n "${FOUNDER_DIR:-}" ]; then
  export CEO_STATE_DIR="${CEO_STATE_DIR:-$FOUNDER_DIR/state}"
else
  export CEO_STATE_DIR="${CEO_STATE_DIR:-${LIFE_MANAGER_STATE_ROOT:-$HOME/.local/state/life-manager/founder-loop-cadence}/state}"
fi
mkdir -p "$CEO_STATE_DIR"

python3 "$HERE/run_pass.py" >>"$CEO_STATE_DIR/ceo-pass.log" 2>&1
exit 0
