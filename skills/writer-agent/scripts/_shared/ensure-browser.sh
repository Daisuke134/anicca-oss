#!/usr/bin/env bash
# ensure-browser.sh — generic CDP-browser watchdog (spec #70b, OSS self-containment).
#
# Bundled, pluggable alternative to the Anicca-instance-specific
# the legacy private browser watchdog, which depends on CloakBrowser (a private,
# stealth-focused Chromium fork that is NOT part of this OSS repo) plus three more
# helper scripts (session_vault.py / cdp_context_lease.py / cdp_tab_gc.py) for cookie
# session recovery and idle-tab garbage collection. Those are legitimate needs for a
# heavily-automated daily loop, but they are specific to whatever private browser
# automation stack an installer already runs -- not something this OSS repo can bundle
# without adopting that private tool wholesale. This script does the one thing every
# browser-driving loop actually needs at minimum: confirm something is listening on
# CDP_PORT, launch BROWSER_BIN if not. Write your own session-recovery/tab-gc layer on
# top of this if your loop needs it (see README in this directory).
#
# Env:
#   CDP_PORT      (default 9222)
#   BROWSER_BIN   (path to a Chromium-family binary; required to actually LAUNCH one --
#                  if unset, this script can still detect ALIVE, just not recover)
#   BROWSER_PROFILE_DIR (default ~/.article-loop-browser-profile) -- MUST be a persistent
#     user-data-dir, not a fresh one every run: the publish scripts in this repo assume
#     an already-logged-in session (note/zenn/substack/x/devto cookies), and a blank
#     profile has none. Provision this persistent profile through the repository-owned
#     browser runtime and log in once before the loop runs unattended.
#
# Usage: bash ensure-browser.sh   -> prints ALIVE (already up) / RECOVERED (relaunched) /
#                                     FAILED (could not confirm a live CDP endpoint)

set -uo pipefail

CDP_PORT="${CDP_PORT:-9222}"
CDP="http://127.0.0.1:${CDP_PORT}"
BROWSER_PROFILE_DIR="${BROWSER_PROFILE_DIR:-$HOME/.article-loop-browser-profile}"

alive() { curl -s --max-time 4 "$CDP/json/version" >/dev/null 2>&1; }

if alive; then
  echo "ALIVE"
  exit 0
fi

if [ -z "${BROWSER_BIN:-}" ] || [ ! -x "${BROWSER_BIN}" ]; then
  echo "FAILED: no live CDP endpoint on port $CDP_PORT, and BROWSER_BIN is not set to an executable Chromium-family binary -- cannot launch one. Set BROWSER_BIN to recover automatically."
  exit 1
fi

mkdir -p "$BROWSER_PROFILE_DIR"
nohup "$BROWSER_BIN" --remote-debugging-port="$CDP_PORT" --user-data-dir="$BROWSER_PROFILE_DIR" \
  --no-first-run --no-default-browser-check >/dev/null 2>&1 &

for _ in 1 2 3 4 5 6 7 8 9 10; do
  sleep 1
  if alive; then
    echo "RECOVERED"
    exit 0
  fi
done

echo "FAILED: launched $BROWSER_BIN but CDP_PORT=$CDP_PORT did not come alive within 10s"
exit 1
