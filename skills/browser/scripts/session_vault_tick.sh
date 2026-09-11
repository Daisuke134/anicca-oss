#!/bin/bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# launchd tick (every 30 min): bank the logins, then warm the server-side sessions so they
# expire less often. Human-zero — reports only, never asks anyone to log in.
#
# Warms the shared daily-driver and the separately owned Gig browser session.
set -uo pipefail
V="$LIFE_MANAGER_REPO/skills/browser/scripts/session_vault.py"
log(){ echo "$(date '+%F %T') session_vault_tick: $*" >&2; }
. "$LIFE_MANAGER_REPO/skills/_shared/scripts/telegram-notify.sh" 2>/dev/null || true

# ── daily-driver (:9222) — unchanged behavior ──
log "daily-driver: dump"
python3 "$V" dump || true
log "daily-driver: keepalive"
# x.com added task #75 (2026-07-17): the article-writer loop's X session died silently for
# hours because nothing warmed/watched it here -- only coconala+instagram were on this list.
# x.com/home relies on the "Something went wrong" content-check fix in session_vault.py.
#
# zenn.dev was ALSO added here during task #75, then REMOVED again in task #76 (same day):
# Zenn publish never uses a browser session at all -- it is a plain `git push` to
# Daisuke134/zenn-articles (SKILL.md "ZENN ONE-SHOT PUBLISH", 2026-06-24), so there is no
# real session to warm or watch, and keepalive's "logged_out" check was reporting a permanent
# false alarm (zenn.dev/dashboard needs a login that this loop never uses or needs). Read the
# base skill's publish mechanism BEFORE adding a platform to this list.
KA_OUT="$(python3 "$V" keepalive \
  "https://coconala.com/mypage/dashboard" \
  "https://www.instagram.com/" \
  "https://x.com/home" || true)"
echo "$KA_OUT"
# alert immediately on any logged_out platform instead of only being discovered hours later by
# the next real business pass (exactly what happened with X today) -- never block/exit on this,
# a notify failure must not break the tick.
DEAD="$(printf '%s' "$KA_OUT" | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
dead = [p['url'] for p in d.get('pages', []) if p.get('logged_out')]
print(','.join(dead))
" 2>/dev/null || true)"
if [ -n "$DEAD" ]; then
  log "ALERT: session dead for: $DEAD"
  telegram_notify "session_vault keepalive: session DEAD for: $DEAD (daily-driver :9222). Will keep retrying every 30min; if a real pass needs it, it will self-heal or report failed per its own STEP 8." || true
fi

# password re-login self-heal for X specifically (task #75): X does not offer a passwordless
# recovery path this loop can drive, but it DOES offer a normal username+password login that
# needs no human -- as long as it is never retried more than once per incident (X flags/kills
# accounts for repeated automated login(), same warning documented in twscrape/twikit). relogin_x
# itself enforces a 6h cooldown marker, so calling it unconditionally here on every tick is safe:
# it silently no-ops (skipped:true) unless x.com is actually dead AND the cooldown has expired.
if printf '%s' "$DEAD" | grep -q "x.com"; then
  set -a; . "$HOME/.local/state/life-manager/.env" 2>/dev/null; set +a
  log "x.com dead -> attempting relogin_x (rate-limited to 1/6h)"
  RELOGIN_OUT="$(python3 "$V" relogin_x || true)"
  echo "$RELOGIN_OUT"
  RELOGIN_STOPPED="$(printf '%s' "$RELOGIN_OUT" | python3 -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print('1' if d.get('stopped') else '')" 2>/dev/null || true)"
  if [ -n "$RELOGIN_STOPPED" ]; then
    log "ALERT: relogin_x stopped itself -- needs Dais (phone verification requested)"
    telegram_notify "X relogin self-heal STOPPED: phone/SMS verification was requested, which this AI cannot complete (SMS goes to Dais's personal phone). Needs Dais to log in manually once. See $RELOGIN_OUT" || true
  fi
fi

# ── gig browser (coconala:kosuke) ──
# The gig Apply/Paid/Storefront lanes do not work in the daily-driver: browser-guard resolves
# coconala:kosuke to its own Chrome on its own profile. :9222 and that browser were once the same
# process behind a proxy, so warming :9222 warmed both; once they were split, nothing warmed the
# gig one and its Coconala session rotted exactly the way a cold clip profile does.
#
# Measured 2026-09-07: /mypage and /offers/add/<id> both redirected to the top page on the gig
# browser while :9222 stayed logged in, so this keepalive reported healthy for four days while
# Coconala applied to nothing. The lane itself said so on every listing -- "公式ページで募集受付中の
# 応募フォームを確認できなかったためです" -- but nothing ever tied that to the session.
#
# The lease is the concurrency contract: BUSY means a gig lane is driving that browser right now,
# which is itself traffic, so skipping is correct and never forces a tab into a live pass.
GIG_GUARD="$HOME/.config/ai/bin/browser-guard.sh"
if [ -x "$GIG_GUARD" ]; then
  # Resolve the live port WITHOUT taking the exclusive lease. `status` reads it from
  # DevToolsActivePort and returns immediately; `acquire` fails whenever any gig lane holds it,
  # which is most of the time -- measured 2026-09-07 07:57:18, "lease BUSY ... skipping this tick"
  # one second after the first successful pass. Gating on the lease means the one job that can heal
  # a dead session almost never runs, which is the opposite of what it is for.
  #
  # Not taking it is correct rather than merely convenient: the four gig lanes already work as
  # several isolated browser contexts on this browser at once (five were live during that
  # measurement), and dump/keepalive/relogin open their own tab and close it. Nothing here waits for
  # a lane, and no lane waits for this.
  log "gig browser: resolve coconala:kosuke port"
  GIG_PORT="$("$GIG_GUARD" status coconala:kosuke 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for row in d.get('identities', []):
    if row.get('identity') == 'coconala:kosuke' and row.get('reachable') and row.get('port'):
        print(row['port']); break
" 2>/dev/null || true)"
  if true; then
    if [ -n "$GIG_PORT" ]; then
      # Bank into the gig browser's OWN vault. SESSION_VAULT_DIR defaults to
      # ~/.cloak/vault/daily-driver, so every dump this tick ever ran wrote the human browser's
      # jar and never the gig one. Measured 2026-09-07: vault/gig-daily-driver/auth-state.json was
      # last written 2026-09-02 02:17 -- the day Coconala's applications stopped. The lane restores
      # its isolated contexts from that file, so it was rehydrating expired cookies every wake and
      # landing on /login. Warming without banking would let it go stale again.
      GIG_VAULT="$HOME/.cloak/vault/gig-daily-driver"
      log "gig browser: dump into $GIG_VAULT"
      SESSION_VAULT_PORT="$GIG_PORT" SESSION_VAULT_DIR="$GIG_VAULT" python3 "$V" dump || true
      log "gig browser: keepalive on :$GIG_PORT"
      GIG_OUT="$(SESSION_VAULT_PORT="$GIG_PORT" SESSION_VAULT_DIR="$GIG_VAULT" python3 "$V" keepalive \
        "https://coconala.com/mypage/dashboard" || true)"
      echo "$GIG_OUT"
      GIG_DEAD="$(printf '%s' "$GIG_OUT" | python3 -c "
import json,sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(0)
print(','.join(p['url'] for p in d.get('pages', []) if p.get('logged_out')))
" 2>/dev/null || true)"
      if [ -n "$GIG_DEAD" ]; then
        log "ALERT: gig browser session dead for: $GIG_DEAD"
        # Heal it. keepalive only extends a session that is still alive; before this there was no
        # re-login path for Coconala at all, only for x.com, so the 2026-09-01 expiry alarmed
        # correctly every 30 minutes for five days and nothing could act on it. Apply, Paid,
        # Storefront and Reply share this one browser and one session, so one login restores all
        # four. relogin_coconala enforces its own 6h cooldown, so calling it every tick is safe:
        # it no-ops with skipped:true until the cooldown expires.
        log "gig browser: attempting relogin_coconala (rate-limited to 1/6h)"
        GIG_RELOGIN="$(SESSION_VAULT_PORT="$GIG_PORT" SESSION_VAULT_DIR="$GIG_VAULT" \
          python3 "$V" relogin_coconala || true)"
        echo "$GIG_RELOGIN"
        GIG_HEALED="$(printf '%s' "$GIG_RELOGIN" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
print('1' if d.get('ok') else '')
" 2>/dev/null || true)"
        if [ -n "$GIG_HEALED" ]; then
          log "gig browser: relogin succeeded, session restored"
          telegram_notify "session_vault: GIG browser (coconala:kosuke) was logged out and has been logged back in automatically. Coconala Apply/Paid/Storefront/Reply resume on the next wake." || true
        else
          telegram_notify "session_vault keepalive: GIG browser (coconala:kosuke :$GIG_PORT) session DEAD for: $GIG_DEAD and automatic relogin did not restore it: $GIG_RELOGIN. This is the browser Apply/Paid/Storefront actually use, so every Coconala application stops until it is logged in again." || true
        fi
      fi
    else
      log "gig browser: coconala:kosuke not reachable, skipping this tick"
    fi
  fi
fi
