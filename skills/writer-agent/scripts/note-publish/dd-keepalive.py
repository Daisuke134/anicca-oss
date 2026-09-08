#!/usr/bin/env python3
"""Reopen + keep the daily-driver CloakBrowser persistent context ALIVE forever.
Per HARD RULE 0.39: never kill/close this. If it died (reboot), relaunch with:
  nohup "$WRITER_BROWSER_PYTHON" <this> &
It opens the daily-driver profile headed (Dais watches via vnc://100.99.82.95),
logged into every service, then sleeps forever so the forever-tab stays up."""
import time
from cloakbrowser import launch_persistent_context

ctx = launch_persistent_context(
    "/Users/anicca/.cloak/profiles/daily-driver",
    headless=False, humanize=True,
    # Chrome's remote-debugging-port already binds loopback-only by default; pin it
    # explicitly for defense-in-depth. --remote-allow-origins=* matches the existing
    # promote-fun-login precedent (launch_promote_browser.py) -- needed because the
    # various local CDP clients connecting to this profile (Playwright, raw curl/ws
    # checks) don't send a matching Origin header, so restricting it would break them.
    # Port 0 = Chrome picks a free port and writes it to DevToolsActivePort inside the
    # profile, which is what browser-guard resolves. Hardcoding a port is what produced
    # the 2026-07-26 collision: the fixed port was already held by a proxy onto the gig
    # production browser, so "the daily driver" silently resolved to another account's
    # Chrome. ~/.config/ai/registry/browsers.toml states the rule -- a port is not an
    # identity, and the live port must be read from the profile, never assumed.
    args=["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1", "--remote-allow-origins=*"],
)
print("daily-driver REOPENED (headed). keeping alive.", flush=True)
while True:
    time.sleep(3600)
