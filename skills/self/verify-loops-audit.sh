#!/usr/bin/env bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# verify-loops-audit.sh — INDEPENDENT scheduled auditor (FIND-016). launchd runs this every 6h. It (1) runs the
# real-side-effect verifier, (2) sends the honest scorecard to the report channel so no-op loops become visible
# (covers life-manager, which has no daily artifact for its own healthcheck), and (3) escalates any loop whose REAL
# output artifact is stale to an autonomous self-fix — grounded in the artifact, never a self-graded marker.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
set -uo pipefail
SELF="${VERIFY_LOOPS_SELF_DIR:-$LIFE_MANAGER_REPO/skills/self}"; now=$(date +%s)
OUT="$(bash "$SELF/verify-loops.sh" 2>&1)"
LOG="$HOME/.local/state/life-manager/logs/verify-loops-audit.log"; mkdir -p "$(dirname "$LOG")"
printf '=== %s ===\n%s\n' "$(date '+%F %T')" "$OUT" >> "$LOG"

stale_hrs(){ [ -f "$1" ] || { echo 99999; return; }; echo $(( (now-$(stat -f %m "$1" 2>/dev/null||echo 0))/3600 )); }
# liveurl (self-heal.md root cause #3 — liveness未配線): duplicated from verify-loops.sh's own
# liveurl() (same convention this file already uses for stale_hrs() vs verify-loops.sh's fresh() —
# these two closely-related scripts each keep their own small helper copy rather than sourcing one
# another). Extracts the URL from the NEWEST record and curls it: LIVE(code)/DEAD(code)/NO-URL.
liveurl(){ local f="$1"; [ -f "$f" ] || { echo "NO-FILE"; return; }
  local u; u="$(tail -1 "$f" 2>/dev/null | grep -oE 'https?://[^"[:space:]]+' | head -1)"
  [ -z "$u" ] && { echo "NO-URL-IN-NEWEST-RECORD"; return; }
  # VERIFY_LOOPS_AUDIT_CURL_BIN test-only override seam (mirrors this codebase's own
  # VERIFY_LOOPS_SELF_DIR/EARN_VIDEO_STATE_PATH/CADENCE_DEADLINE_NOW_HOUR_JST convention) so a test
  # can stub the HTTP outcome without touching the real network or fighting this script's own
  # hardcoded PATH= line (which always puts the real system curl ahead of any PATH-based stub).
  # UA (self-fix 2026-07-11): reddit.com/old.reddit.com returns 403 to curl's default bare
  # User-Agent even for genuinely-live posts (confirmed live: same URL returns 200 with a
  # browser UA) — a false DEAD would have spuriously re-triggered self-fix on a healthy loop.
  local code; code="$("${VERIFY_LOOPS_AUDIT_CURL_BIN:-curl}" -s -o /dev/null -w '%{http_code}' --max-time 12 -L -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36' "$u" 2>/dev/null||echo 000)"
  case "$code" in 200|201|202|301|302|308) echo "LIVE($code) $u";; *) echo "DEAD($code) $u";; esac; }
# reddit_username (G2 item1, self-heal.md root cause #3 continuation): newest posts.jsonl record's
# own "account" field -- the account that actually made the newest post, never guessed.
reddit_username(){ local f="$1"; [ -f "$f" ] || { echo ""; return; }
  python3 -c "
import json
try:
    for line in reversed(open('$f').read().splitlines()):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        u = d.get('account')
        if u:
            print(u)
            break
except Exception:
    pass
" 2>/dev/null; }
# reddit_account_banned (G2 item1, 2026-07-11 loop-arch redesign / LOOPS-TRUTH-AUDIT.md: "liveurl
# HTTP200のみ→reddit BAN...を全て見逃す"): a 200 on the post URL itself proves nothing about the
# ACCOUNT. CORRECTED 2026-07-12 (self-fix): the prior version here treated a 404 with title
# "u/<user>: page not found" as the BAN signal ("confirmed banned 2026-07-11" via curl+screenshot).
# That was a FALSE POSITIVE, reproduced and disproven 2026-07-12: an authenticated camofox browser
# session loading the SAME anicca_sao account's own overview page rendered it completely normally
# -- live comment posted "18 hours ago" matching posts.jsonl exactly, edit/delete/reply controls
# present, nothing suspended. old.reddit.com returns that identical 404 + "u/<user>: page not
# found" pattern for ANY profile it won't show to a logged-out/unauthenticated curl request
# (reproduced on the live, active, unbanned anicca_sao account itself) -- it is a visibility
# restriction (e.g. low karma / newer account), not a ban page, and cannot be used as a BAN
# signal. The REAL, distinctive ban signature was established 2026-07-12 against a well-documented
# permanently-suspended account (u/violentacrez): HTTP 403 with title "reddit.com: suspended". A
# random never-registered username also 404s but with a generic "reddit.com: page not found"
# title (no username echoed) -- so 404 alone, with or without the username in the title, means
# "not visible to an anonymous curl request", NOT banned.
reddit_account_banned(){ local user="$1"; [ -n "$user" ] || { echo "NO-USER"; return; }
  local tmp; tmp="$(mktemp)"
  local code; code="$("${VERIFY_LOOPS_AUDIT_CURL_BIN:-curl}" -s -o "$tmp" -w '%{http_code}' --max-time 12 -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36' "https://old.reddit.com/user/$user/" 2>/dev/null||echo 000)"
  local title; title="$(grep -o -m1 -iE '<title>[^<]*</title>' "$tmp" 2>/dev/null)"
  rm -f "$tmp"
  if [ "$code" = "403" ] && printf '%s' "$title" | grep -qi "suspended"; then
    echo "BANNED(403) u/$user"
  else
    echo "OK($code) u/$user"
  fi
}
CAP="$LIFE_MANAGER_REPO/skills/capafy-autopublish/state/published.jsonl"
POSTS="${REDDIT_STATE_DIR:-$HOME/.local/state/life-manager/reddit/state}/posts.jsonl"
LMHB="$HOME/.local/state/life-manager/state/.life-manager-loop-last-pass"

# capafy_live_verdict (self-fix 2026-07-19): consult the daily loop's OWN server-truth verdict
# (inventory_status.py, added 2026-07-08 specifically to tell healthy-idle DRAINED/CAP_FULL apart
# from a genuinely broken pipeline) before escalating on published.jsonl staleness alone. Without
# this, CAP_FULL (Capafy's own 5-slot review cap saturated -- an external, non-code condition) fires
# this alarm every 6h forever once the cap fills, spawning a self-fix that finds nothing to fix
# (reproduced live 2026-07-19: unlisted=5/5, verdict=CAP_FULL, all 5 genuinely under_review on
# Capafy's side -- exactly the false-alarm class inventory_status.py's own docstring describes, just
# never wired into THIS separate 6h audit script). Empty/unreadable verdict (script missing or
# errors, e.g. under test) fails OPEN to the old behavior (escalate) so a real break is never
# silently swallowed.
INV_SCRIPT="$LIFE_MANAGER_REPO/skills/capafy-autopublish/scripts/inventory_status.py"
CAPAFY_VERDICT=""
[ -f "$INV_SCRIPT" ] && CAPAFY_VERDICT="$(python3 "$INV_SCRIPT" 2>/dev/null | grep -o '^VERDICT=[A-Z_]*' | cut -d= -f2)"

# escalate stale real outputs (only via self-fix.sh, which self-guards against duplicate/hung fixers)
if [ "$(stale_hrs "$CAP")" -ge 30 ] && [ "$CAPAFY_VERDICT" != "DRAINED" ] && [ "$CAPAFY_VERDICT" != "CAP_FULL" ]; then
  bash "$SELF/self-fix.sh" capafy "audit: no new capafy skill published in >30h (published.jsonl stale, live verdict=${CAPAFY_VERDICT:-UNKNOWN}). Fix the publish pipeline so a real skill lands." >> "$LOG" 2>&1 || true
fi
# reddit: only escalate if an account exists (else it is legitimately pre-provision). self-heal.md
# root cause #3: a DEAD newest-post URL (e.g. the real 07-xx reddit 403) used to sit unrepaired
# forever because ONLY stale_hrs was checked here — a stale-but-alive OR a fresh-but-dead post both
# need repair, so this is now an OR, not stale_hrs alone.
NACC=0; [ -f "$HOME/.cloak/reddit-accounts.json" ] && NACC="$(python3 -c "import json;d=json.load(open('$HOME/.cloak/reddit-accounts.json'));print(len(d if isinstance(d,list) else d.get('accounts',[])))" 2>/dev/null||echo 0)"
REDDIT_STALE=0; [ "$(stale_hrs "$POSTS")" -ge 30 ] 2>/dev/null && REDDIT_STALE=1
REDDIT_DEAD=0; case "$(liveurl "$POSTS")" in DEAD*) REDDIT_DEAD=1;; esac
# G2 item1: account-level BAN check (independent of any single post's own HTTP code -- a banned
# account can still show a stale-but-200 post while being invisible/unusable going forward).
REDDIT_USER="$(reddit_username "$POSTS")"
REDDIT_BANNED=0; REDDIT_BAN_STATUS="skipped(no account field in posts.jsonl)"
if [ -n "$REDDIT_USER" ]; then
  REDDIT_BAN_STATUS="$(reddit_account_banned "$REDDIT_USER")"
  case "$REDDIT_BAN_STATUS" in BANNED*) REDDIT_BANNED=1;; esac
fi
{ [ "$NACC" -ge 1 ] 2>/dev/null && { [ "$REDDIT_STALE" = 1 ] || [ "$REDDIT_DEAD" = 1 ] || [ "$REDDIT_BANNED" = 1 ]; }; } && bash "$SELF/self-fix.sh" reddit "audit: reddit has an account but no real post in >30h (posts.jsonl stale=$REDDIT_STALE) or the newest post URL is DEAD (dead=$REDDIT_DEAD) or the account itself is BANNED/suspended (banned=$REDDIT_BANNED, $REDDIT_BAN_STATUS). If banned, posting more from this same account cannot work -- get a new honest disclosed account (see skills/reddit/loop.sh's NO-REDDIT-ACCOUNT heal path) rather than retrying the dead one; otherwise make it post one honest disclosed contribution and log the URL." >> "$LOG" 2>&1 || true

# G2 item2 (2026-07-11 loop-arch redesign / LOOPS-TRUTH-AUDIT.md "video: 2つの state file(warmup_day
# 4 vs 0)の整合性チェックが無い"): video's warmup_day is tracked in TWO independently-written
# files that can silently diverge -- run.sh's own gating state ~/.cloak/earn-video-<handle>.json
# (what decide.py actually reads to choose S1_warmup vs post) and the separately-maintained
# ~/.cloak/ig-warmup-<handle>.json (written by the ig-account-warmer skill's own warm_iso.py/
# warm.py). Confirmed real drift 2026-07-11: earn-video-money_blueprintdaily.json warmup_day=4 vs
# ig-warmup-money_blueprintdaily.json warmup_day=0, same last_warmup_date -- nothing detected it
# before now. Escalates to self-fix so the two trackers get reconciled to one source of truth.
for VF in "$HOME"/.cloak/earn-video-*.json; do
  [ -f "$VF" ] || continue
  V_HANDLE="$(python3 -c "
import json
try:
    print(json.load(open('$VF')).get('handle') or '')
except Exception:
    print('')" 2>/dev/null)"
  [ -n "$V_HANDLE" ] || continue
  IGF="$HOME/.cloak/ig-warmup-${V_HANDLE}.json"
  [ -f "$IGF" ] || continue
  V_DAY="$(python3 -c "
import json
try:
    d = json.load(open('$VF')).get('warmup_day')
    print(d if d is not None else 'NA')
except Exception:
    print('NA')" 2>/dev/null)"
  IG_DAY="$(python3 -c "
import json
try:
    d = json.load(open('$IGF')).get('warmup_day')
    print(d if d is not None else 'NA')
except Exception:
    print('NA')" 2>/dev/null)"
  if [ "$V_DAY" != "NA" ] && [ "$IG_DAY" != "NA" ] && [ "$V_DAY" != "$IG_DAY" ]; then
    bash "$SELF/self-fix.sh" video "audit: warmup_day state DRIFT for handle=$V_HANDLE -- $VF (the file run.sh/decide.py actually gate on) says warmup_day=$V_DAY, but $IGF (a second, separately-written warmup tracker) says warmup_day=$IG_DAY. These must be reconciled to ONE source of truth -- either make run.sh read $IGF instead, make the ig-account-warmer skill stop writing its own copy, or make one file derive from the other -- so warmup progress is never ambiguous again." >> "$LOG" 2>&1 || true
  fi
done

# G2 item3 (2026-07-11 loop-arch redesign / LOOPS-TRUTH-AUDIT.md "capafy: 'PUBLISHED/DRAINED'等
# ラベルの誤表示を実side-effect...で照合してない"): confirmed real incident 2026-07-11 --
# daily_loop.log's own "done" line read '...rc=1 (PUBLISHED — post-verdict=DRAINED, marker
# touched)' right after 'Error: Reached max turns' and a reconcile pass with appended=0 -- PUBLISHED
# there is a static post-verdict label (echoing the PRE-run verdict), not proof a listing actually
# went live. Detect the mismatch directly against the real ledger: if the newest 'daily_loop done'
# log line claims PUBLISHED but published.jsonl gained zero new row today (JST) whose own status
# shows a genuinely live listing (contains "status=4" or "online"), the label is lying.
CAPLOG="$LIFE_MANAGER_REPO/skills/capafy-autopublish/state/daily_loop.log"
if [ -f "$CAPLOG" ]; then
  CAP_LAST_DONE="$(grep -E 'daily_loop done' "$CAPLOG" 2>/dev/null | tail -1)"
  case "$CAP_LAST_DONE" in
    *PUBLISHED*)
      CAP_NEW_LIVE_TODAY="$(TODAY_JST_FOR_CAP="$(TZ=Asia/Tokyo date +%F)" python3 -c "
import json, os
today = os.environ['TODAY_JST_FOR_CAP']
n = 0
try:
    for line in open('$CAP'):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        status = str(d.get('status', ''))
        if str(d.get('date', '')) == today and ('status=4' in status or 'online' in status.lower()):
            n += 1
except Exception:
    pass
print(n)" 2>/dev/null)"
      CAP_NEW_LIVE_TODAY="${CAP_NEW_LIVE_TODAY:-0}"
      if [ "$CAP_NEW_LIVE_TODAY" = "0" ] 2>/dev/null; then
        bash "$SELF/self-fix.sh" capafy "audit: daily_loop.log's newest 'daily_loop done' line claims PUBLISHED ($CAP_LAST_DONE) but published.jsonl gained ZERO new row today (JST) whose status shows a real live listing (status=4/online). The PUBLISHED label does not correspond to a real new publish this run -- find where daily_loop.sh emits that label (it looks like it is echoing the pre-run inventory post-verdict, not a genuine outcome) and fix it so it only says PUBLISHED when a real new listing actually went live; also check whether today's publish attempt needs to be re-run." >> "$LOG" 2>&1 || true
      fi
      ;;
  esac
fi

# LM has no daily artifact; if its liveness heartbeat is stale the healthcheck restarts it — the audit surfaces both
# the staleness (in HOURS, FIND-022 bug fix: was interpolating the file PATH) and the live Stripe MRR so a no-op LM
# loop is visible in every 6h report.
LMH="$(stale_hrs "$LMHB")"
LM_MRR="$(grep -E '^lm_mrr_usd:' "$SELF/life-manager-loop/state/STATE.md" 2>/dev/null|awk '{print $2}'|tail -1)"; LM_MRR="${LM_MRR:-NA}"
if [ "$LMH" -ge 26 ] 2>/dev/null; then LM_NOTE=" | ⚠ LM last-pass STALE ${LMH}h, mrr=\$$LM_MRR"; else LM_NOTE=" | LM last-pass ${LMH}h, mrr=\$$LM_MRR"; fi

# --- REQ-LV-102/103/104: Cadence Contract escalation + scorecard for the 7 contract loops. This
# REPLACES the old fresh()/stale_hrs() judgment for these 7 loops ONLY — capafy/reddit/lm (above)
# keep stale_hrs()/self-fix unchanged (REQ-LV-104, out of this feature's scope).
STATE_DIR="$HOME/.local/state/life-manager/state"; mkdir -p "$STATE_DIR"
TODAY_JST="$(TZ=Asia/Tokyo date +%F)"
CADENCE_LOOPS="clip affiliate video gig bounty pm-earner founder-loop"
CADENCE_SCORECARD=""
for L in $CADENCE_LOOPS; do
  STATUS_JSON="$(python3 "$SELF/cadence-evidence.py" status "$L" 2>>"$LOG")"
  SCORECARD_LINE="$(printf '%s' "$STATUS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['scorecard'])" 2>/dev/null || echo "❌missed (streak=0)")"
  CADENCE_SCORECARD="$CADENCE_SCORECARD [$L] $SCORECARD_LINE;"
done

# REQ-LV-102 (F-ITER3-1 fix): the 21:00 JST escalation itself now lives in its own script, run by
# a dedicated StartCalendarInterval(Hour=21,Minute=5,JST) launchd job (skills/self/launchd/
# ai.anicca.cadence-deadline-check.plist) so it is GUARANTEED to fire once/day at a fixed wall-clock
# time — this 6h script's own rolling StartInterval could, depending on launchd-load-time offset,
# never once land inside [21:00,24:00) on a given day (the bug iteration-3 review caught). Still
# called here too as a redundant, marker-gated (harmless) safety net.
bash "$SELF/cadence-deadline-check.sh" >> "$LOG" 2>&1 || true

# --- REQ-LV-111: weekly EDD evaluator run (once per ISO week per loop, marker-gated — this
# every-6h script would otherwise re-run it up to 28x/week for no reason). Scores this-week vs
# last-week via each loop's own evaluator.py (REQ-LV-110), records beats_previous_week to the
# loop's OWN metrics ledger. This SCRIPT only measures/records the weekly comparison — deciding
# WHAT to change based on a losing week stays with the agent (REQ-LV-113's STARTUP prompt text).
EDD_LOOPS="clip affiliate video gig bounty"
DOW_JST="$(TZ=Asia/Tokyo date +%u)"   # 1=Mon..7=Sun
THIS_MONDAY_JST="$(TZ=Asia/Tokyo date -v-$((DOW_JST-1))d +%F 2>/dev/null || echo "$TODAY_JST")"
for L in $EDD_LOOPS; do
  MK="$STATE_DIR/.weekly-eval-$L-$THIS_MONDAY_JST"
  if [ ! -f "$MK" ]; then
    touch "$MK"
    python3 "$SELF/self-improve/weekly_report.py" "$L" >> "$LOG" 2>&1 || true
  fi
done

# send the honest scorecard to the report channel (visibility = no-op auto-detection for every loop incl LM).
# self-heal.md root cause #2: 900 chars truncated $OUT before the self-fix marker section (added
# above, "--- self-fix result markers ---") ever printed, so an honest FAIL diagnosis (e.g.
# affiliate's reCAPTCHA wall #994, bounty's sourcing exhaustion #995) never reached the mail body.
# 3000 chars comfortably covers verify-loops.sh's full output incl. all 10 loops' self-fix markers;
# AgentMail's text body has no practical length limit that would make this unsafe (loop-report.sh
# just JSON-encodes it into a plain 'text' field).
if [ -x "$SELF/../report/loop-report.sh" ]; then
  bash "$SELF/../report/loop-report.sh" audit "$(printf '%s' "$OUT" | tr '\n' ' ' | cut -c1-3000)$LM_NOTE |$CADENCE_SCORECARD" no-op 0 "none: routine 6h scorecard, no per-pass artifact" >> "$LOG" 2>&1 || true
fi
# AGENTIC verification (#5 last mile): after the deterministic side-effect audit above, fire the
# fresh-context reality-verifier on any loop that booked a NEW external:true earn since last check.
# Token-safe: spawns nothing when there are no new earns (today's state). Fail-safe: never breaks this audit.
bash "$SELF/reality-verify-on-new-earn.sh" >> "$LOG" 2>&1 || true

echo "[verify-loops-audit] done $(date '+%F %T')"
