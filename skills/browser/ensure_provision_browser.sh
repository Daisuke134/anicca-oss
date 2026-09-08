#!/usr/bin/env bash
# ensure_provision_browser.sh — bring up a DEDICATED CloakBrowser for account provisioning and
# hand back its leased CDP base URL.
#
# WHY THIS EXISTS (root cause, measured 2026-07-30): capafy-ig-marketing-daily.sh hardcoded
# IG_PROVISION_PORT=9332 and NOTHING in any skill ever launched a browser on 9332. Every pass since
# 2026-07-19 therefore stopped at "dedicated CDP port 9332 was unavailable", wrote a provision_failed
# row, and exited 0 — 11 silent days with zero posts. A port is not a browser and a port is not an
# identity: ~/.config/ai/registry/browsers.toml says "Ports are derived at runtime (launch with
# --remote-debugging-port=0 and read the real one from DevToolsActivePort inside the profile). Never
# hardcode a port". This script is the missing launcher for that doctrine.
#
#   bash ensure_provision_browser.sh <identity>     # -> stdout: http://127.0.0.1:<live-port>
#
# Exit 0 = browser is up on a dynamically allocated free port AND this caller holds the guard lease.
# Any non-zero exit means the caller MUST NOT proceed to provisioning (and must not exit 0 itself).
#
# ★ It never touches the daily-driver (interactive:dais) or gig (coconala:kosuke) profile. Those are
#   forever browsers holding Dais's logins; killing or stealing one is the cardinal sin. This script
#   only ever launches/repairs its OWN provisioning profile and refuses outright if the identity it
#   was given resolves to a protected profile. ★
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

IDENTITY="${1:?identity is required (see ~/.config/ai/registry/browsers.toml)}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="${AI_BROWSER_GUARD:-$HERE/browser-guard.sh}"
REGISTRY="${AI_BROWSER_REGISTRY:-$HOME/.config/ai/registry/browsers.toml}"
KEEPALIVE="$HERE/cdp_persistent_context.py"
CLOAK_PY="${CLOAK_PYTHON:-${LIFE_MANAGER_PYTHON:-$HOME/.local/share/life-manager/venv/bin/python}}"
LOG="${PROVISION_BROWSER_LOG:-$HOME/.local/state/life-manager/provision-browser/logs/provision-browser.log}"
LAUNCH_WAIT="${PROVISION_BROWSER_WAIT:-120}"
PY="/opt/homebrew/bin/python3"; [ -x "$PY" ] || PY=python3
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true

log() { echo "$(date '+%F %T') ensure_provision_browser[$IDENTITY]: $*" >>"$LOG"; }
fail() { log "FAILED: $*"; echo "ensure_provision_browser: $*" >&2; exit 1; }

# Protected profiles: forever browsers owned by other identities. Never launch, never clean, never
# steal. Anything that would point this launcher at one of them is a bug, so fail closed.
PROTECTED_PROFILES="$HOME/.cloak/profiles/daily-driver
$HOME/.cloak/profiles/gig-daily-driver"

[ -x "$GUARD" ] || fail "browser-guard.sh not found at $GUARD"
[ -f "$KEEPALIVE" ] || fail "persistent-context owner script missing: $KEEPALIVE"
[ -x "$CLOAK_PY" ] || fail "CloakBrowser runtime missing: $CLOAK_PY"

# ---- registry lookup (same minimal TOML read browser-guard.sh uses; registry stays the SSOT) ----
profile="$("$PY" - "$REGISTRY" "$IDENTITY" <<'PYEOF'
import os, re, sys
registry, want = sys.argv[1], sys.argv[2]
try:
    text = open(os.path.expanduser(registry), encoding="utf-8").read()
except OSError:
    raise SystemExit(0)
for block in text.split("[[identity]]")[1:]:
    ident = re.search(r'^id\s*=\s*"([^"]*)"', block, re.M)
    prof = re.search(r'^profile\s*=\s*"([^"]*)"', block, re.M)
    if ident and ident.group(1) == want and prof:
        print(os.path.expanduser(prof.group(1)))
        break
PYEOF
)"
[ -n "$profile" ] || fail "identity '$IDENTITY' is not in $REGISTRY — add an [[identity]] block first"

while IFS= read -r protected; do
  [ -n "$protected" ] || continue
  if [ "$profile" = "$protected" ]; then
    fail "identity '$IDENTITY' resolves to PROTECTED profile $profile — refusing (forever browser)"
  fi
done <<EOF
$PROTECTED_PROFILES
EOF

LABEL="ai.anicca.provision-browser.$(printf '%s' "$IDENTITY" | tr '/:' '..' | tr -cd 'A-Za-z0-9._-')"

# Reachability is asked through the guard, never with a raw CDP curl of our own: the guard resolves
# the live port from DevToolsActivePort and verifies the browser UUID is not another identity's.
reachable() {
  bash "$GUARD" status "$IDENTITY" 2>/dev/null | "$PY" -c 'import json,sys
try:
    rows = json.load(sys.stdin).get("identities") or []
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if rows and rows[0].get("reachable") else 1)'
}

live_port() {
  bash "$GUARD" status "$IDENTITY" 2>/dev/null | "$PY" -c 'import json,sys
try:
    rows = json.load(sys.stdin).get("identities") or []
    print(rows[0].get("port") or "")
except Exception:
    print("")'
}

# Chromium leaves these singletons behind when it dies uncleanly; a later launch then exits instantly
# with "existing browser session". Only ever for OUR profile, and only when no process owns it.
clear_stale_singletons() {
  if pgrep -f "user-data-dir=$profile" >/dev/null 2>&1; then
    log "not clearing singletons: a process still owns $profile"
    return 0
  fi
  rm -f "$profile/SingletonLock" "$profile/SingletonSocket" "$profile/SingletonCookie" 2>/dev/null || true
  log "cleared stale Chromium singleton artifacts for $profile"
}

launch() {
  if ! "$CLOAK_PY" "$KEEPALIVE" --profile "$profile" --port 0 --preflight-only >>"$LOG" 2>&1; then
    log "disk preflight failed before touching provisioning profile $profile"
    return 1
  fi
  mkdir -p "$profile" 2>/dev/null || true
  clear_stale_singletons
  launchctl remove "$LABEL" 2>/dev/null || true
  sleep 1
  # --port 0 = let the kernel hand us a genuinely free port; Chromium writes the real one into
  # DevToolsActivePort, which the guard reads. This is why no port is ever hardcoded again.
  if ! launchctl submit -l "$LABEL" -o "$LOG" -e "$LOG" -- \
      "$CLOAK_PY" "$KEEPALIVE" --profile "$profile" --port 0; then
    return 1
  fi
  log "submitted persistent-context owner label=$LABEL profile=$profile port=dynamic"
}

if reachable; then
  log "ALIVE on :$(live_port)"
else
  log "not reachable -> launching dedicated provisioning browser"
  launch || fail "launchctl could not submit $LABEL"
  waited=0
  until reachable; do
    if [ "$waited" -ge "$LAUNCH_WAIT" ]; then
      fail "dedicated browser for '$IDENTITY' did not answer /json/version within ${LAUNCH_WAIT}s (profile=$profile label=$LABEL)"
    fi
    sleep 3
    waited=$((waited + 3))
  done
  log "RECOVERED: up on :$(live_port) after ${waited}s"
fi

# The lease is the contract the rest of the system checks; acquiring it is part of "ready".
url="$(bash "$GUARD" acquire "$IDENTITY" 2>>"$LOG")" || {
  rc=$?
  [ "$rc" = "9" ] && fail "guard BUSY: another holder owns '$IDENTITY' — skip this pass"
  fail "guard refused '$IDENTITY' (exit $rc) — see $LOG"
}
log "leased $url"
printf '%s\n' "$url"
