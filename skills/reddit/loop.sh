#!/usr/bin/env bash
# reddit-loop/loop.sh — ONE no-human wake of the Reddit demand-gen loop (feeds life-manager-loop). This is NOT
# revenue; it measures the DEMAND engine's health/progress honestly. Anti-fake: a signup/post counts only if a real
# marker (posts.jsonl / attributed-signups.jsonl) exists; karma comes from the account ledger; nothing is invented.
# Seams: RD_TEST=1 + RD_ACCOUNTS=<file> + RD_DIR + RD_REQ.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
if [ -n "${RD_DIR:-}" ]; then STATE="${RD_DIR}/state"; else STATE="${REDDIT_STATE_DIR:-$HOME/.local/state/life-manager/reddit/state}"; fi
STATE_MD="$STATE/STATE.md"; mkdir -p "$STATE"
REQ="${RD_REQ:-$STATE/reddit-loop-selfheal-request.json}"
ACCTS="${RD_ACCOUNTS:-$HOME/.cloak/reddit-accounts.json}"
POSTS="$STATE/posts.jsonl"; SIGN="$STATE/attributed-signups.jsonl"
now=$(date +%s)
# FIND-010: safe non-empty-line count (never chain ||echo after grep -c).
count(){ [ -f "$1" ] || { echo 0; return; }; local n; n="$(grep -c . "$1" 2>/dev/null)"; echo "${n:-0}"; }
HEAL=""; add_heal(){ HEAL="$HEAL$1; "; }
# HEAL: at least one account? (creation is honest — NEVER covertly bypass Reddit's signup CAPTCHA; a one-time human
# CAPTCHA relay is the allowed human=compute step, or operate an existing disclosed builder account.)
N_ACCT=0
[ -f "$ACCTS" ] && N_ACCT="$(python3 -c "import json;d=json.load(open('$ACCTS'));print(len(d if isinstance(d,list) else d.get('accounts',[])))" 2>/dev/null||echo 0)"
[ "$N_ACCT" -ge 1 ] 2>/dev/null || add_heal "NO-REDDIT-ACCOUNT → operate an existing disclosed builder account, or create one honestly (normal signup; if a CAPTCHA blocks it, that ONE step is a human=compute relay — do NOT auto-bypass anti-bot controls); meanwhile use self-promo-welcome subs' scheduled threads"
# HEAL: camofox reachable? (Reddit is hard-blocked on the shared CDP :9222 daily-driver — this
# account lives ONLY on camofox :9377, see ~/.cloak/reddit-accounts.json; checking :9222 here would
# heal the wrong browser and misdirect the next pass.)
if [ "${RD_TEST:-}" != "1" ]; then
  curl -s --max-time 5 -o /dev/null http://127.0.0.1:9377/health 2>/dev/null || add_heal "CAMOFOX-DOWN(:9377) → bash $REPO_ROOT/skills/camofox-browser/scripts/start.sh"
fi
# READ: karma + posts + signups (all from real markers)
KARMA="$(python3 -c "import json;d=json.load(open('$ACCTS'));a=(d if isinstance(d,list) else d.get('accounts',[]));print(sum(int(x.get('comment_karma',0)) for x in a))" 2>/dev/null||echo 0)"
NPOST="$(count "$POSTS")"; SIGNUPS="$(count "$SIGN")"
PREV="$(grep -E '^comment_karma_total:' "$STATE_MD" 2>/dev/null|awk '{print $2}'|tail -1)"; PREV="${PREV:-n/a}"
# FIND-008: posts-freshness guard — an account with NO post in >30h is a stalled demand engine. Keep this threshold
# aligned with verify-loops-audit.sh so the deterministic measure cannot report healthy while the audit escalates.
# publish-loop-freshness death guard), so the loop's own MEASURE surfaces it rather than looking healthy.
POST_FRESH="never"
if [ -f "$POSTS" ] && [ "$NPOST" -gt 0 ] 2>/dev/null; then
  # Do not use the ledger file mtime here. A pass, checkout, or editor can rewrite/touch
  # posts.jsonl without adding a Reddit contribution, masking a stale newest post.
  latest_post_epoch="$(python3 - "$POSTS" <<'PY'
import datetime, json, sys

path = sys.argv[1]
latest = 0
with open(path, encoding="utf-8") as fh:
    for line in fh:
        try:
            value = json.loads(line).get("ts")
            if not isinstance(value, str):
                continue
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            parsed = datetime.datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            latest = max(latest, int(parsed.timestamp()))
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
print(latest)
PY
  )"
  if [ "${latest_post_epoch:-0}" -le 0 ] 2>/dev/null; then
    latest_post_epoch="$(stat -f %m "$POSTS" 2>/dev/null || echo 0)"
  fi
  age_h=$(( (now-latest_post_epoch)/3600 )); [ "$age_h" -lt 30 ] && POST_FRESH="fresh(${age_h}h)" || POST_FRESH="REDDIT-POSTS-STALE(${age_h}h)"
fi
if [ -n "$HEAL" ]; then STATUS="HEAL-NEEDED — ${HEAL}"
elif [ "$NPOST" -eq 0 ] 2>/dev/null; then STATUS="ACT-FIRST-POST — no post yet: make ONE honest DISCLOSED builder post in a self-promo-welcome sub (r/SideProject / r/SomebodyMakeThis / relevant feedback thread), 'I built a small ADHD-lateness tool, free to try, feedback welcome'. Log it to posts.jsonl."
elif [ "$POST_FRESH" != "${POST_FRESH#REDDIT-POSTS-STALE}" ]; then STATUS="STALE — $POST_FRESH: engine stalled, make ONE honest disclosed contribution this pass and log to posts.jsonl"
else STATUS="ACT — post ONE more honest DISCLOSED contribution / answer genuine replies; no karma-farming, no covert shill. Log real posts→posts.jsonl, real signups→attributed-signups.jsonl (karma=$KARMA informational)"; fi
if [ -n "$HEAL" ]; then mkdir -p "$(dirname "$REQ")"; printf '{"loop":"reddit","ts":"%s","heal":"%s"}\n' "$(date -u +%FT%TZ)" "${HEAL//\"/}" > "$REQ"; else rm -f "$REQ" 2>/dev/null||true; fi
TMP="$STATE_MD.tmp.$$"
{
  echo "# Reddit demand-gen loop — STATE (feeds life-manager-loop; HONEST DISCLOSED participation, NOT covert/broadcast)"
  echo "goal: drive REAL LM signups via transparent builder participation. A post/signup counts ONLY if logged with a real URL."
  echo "last_wake_utc: $(date -u +%FT%TZ)"
  echo "heal_first: ${HEAL:-account present ✓, camofox ✓}"
  echo "reddit_accounts: $N_ACCT"
  echo "comment_karma_total: $KARMA"
  echo "prev_comment_karma_total: $PREV"
  echo "posts_made: $NPOST"
  echo "posts_freshness: $POST_FRESH"
  echo "attributed_lm_signups: $SIGNUPS"
  echo "status: $STATUS"
  echo "selfheal_request: ${HEAL:+written→$REQ}${HEAL:-none}"
  echo "next: HEAL→get an honest account + CloakBrowser up; else post ONE disclosed builder contribution in a self-promo-welcome sub and LOG it to posts.jsonl (real URL); answer genuine replies; log any reddit→LM signup to attributed-signups.jsonl. Never covert, never ask a human."
} > "$TMP" && mv "$TMP" "$STATE_MD"
echo "[reddit-loop] accounts=$N_ACCT karma=$KARMA posts=$NPOST($POST_FRESH) signups=$SIGNUPS | heal=${HEAL:-none} | $STATUS"

# Liveness heartbeat (FIND-032, ported from life-manager-loop): touch it HERE, in the deterministic MEASURE
# core (runs first on every pass — startup + daily cron — via STEP1, completes in ~2s, cannot derail).
# Previously the reddit heartbeat was touched ONLY at the very end of the open-ended STARTUP/cron pass
# ("FINALLY touch"), AFTER STEP2 ACT — and Reddit ACT routinely stalls (fresh top-level posts auto-removed
# by the spam filter at low karma; mod-message Send button client-side-disabled; an occasional interactive
# prompt) so the pass burns its whole 1800s timeout and never returns to the touch. That starved the
# heartbeat → healthcheck STALE(1560m) → restart → pkill kills the in-progress claude → fresh claude
# re-enters the same stalling ACT → still no touch in the 5-min window → STALE again → restart storm →
# give-up → self-fix. The healthcheck contract is "liveness + stuck-detection only" (real posting truth =
# posts.jsonl freshness, checked separately by the output guard), so liveness must mean "the loop executed
# its measurable core this pass", NOT "the open-ended posting task completed". Skip under test mode so the
# real prod heartbeat is never touched by test-loop.sh.
[ "${RD_TEST:-}" = "1" ] || touch "$STATE/.reddit-loop-last-pass" 2>/dev/null || true
