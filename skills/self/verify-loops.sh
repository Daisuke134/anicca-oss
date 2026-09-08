#!/usr/bin/env bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# verify-loops.sh — proves the 3 loops produce REAL side-effects (not just ALIVE). Anti-fake: reads only observable
# artifacts AND, when an artifact records a URL, actually CURLS it to confirm the thing is live (FIND-007) — so a
# fabricated log line cannot pass. Never trusts a loop's STATE self-claim.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
set -uo pipefail
now=$(date +%s)
# FIND-010: safe non-empty-line count (grep -c prints "0" on no match; do NOT chain ||echo which double-prints).
count(){ [ -f "$1" ] || { echo 0; return; }; local n; n="$(grep -c . "$1" 2>/dev/null)"; echo "${n:-0}"; }
fresh(){ local f="$1" hrs="${2:-26}"; [ -f "$f" ] || { echo "MISSING"; return; }; local m h; m=$(stat -f %m "$f" 2>/dev/null||echo 0); h=$(( (now-m)/3600 )); [ "$h" -le "$hrs" ] && echo "FRESH(${h}h)" || echo "STALE(${h}h)"; }
# extract the URL from the NEWEST record only (FIND-019: tie the check to the latest post/skill, not any old line),
# then curl it: LIVE(code) / DEAD(code) / NO-URL.
liveurl(){ local f="$1"; [ -f "$f" ] || { echo "NO-FILE"; return; }
  local u; u="$(tail -1 "$f" 2>/dev/null | grep -oE 'https?://[^"[:space:]]+' | head -1)"
  [ -z "$u" ] && { echo "NO-URL-IN-NEWEST-RECORD"; return; }
  local code; code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 -L "$u" 2>/dev/null||echo 000)"
  case "$code" in 200|201|202|301|302|308) echo "LIVE($code) $u";; *) echo "DEAD($code) $u";; esac; }

echo "=== LOOP REAL-SIDE-EFFECT VERIFICATION ($(date '+%F %H:%M')) ==="
# 1 CAPAFY: a skill actually published + the newest listing URL actually live
PUB="$LIFE_MANAGER_REPO/skills/capafy-autopublish/state/published.jsonl"
echo "[capafy]  published: $(count "$PUB") skills | newest-line: $(fresh "$PUB") | live-check: $(liveurl "$PUB")"
echo "          → PASS only if count grows daily AND newest listing URL is LIVE"
# 2 REDDIT: a real post made + its URL live + an account exists
ACC="$HOME/.cloak/reddit-accounts.json"; POSTS="${REDDIT_STATE_DIR:-$HOME/.local/state/life-manager/reddit/state}/posts.jsonl"
NACC=0; [ -f "$ACC" ] && NACC="$(python3 -c "import json;d=json.load(open('$ACC'));print(len(d if isinstance(d,list) else d.get('accounts',[])))" 2>/dev/null||echo 0)"
echo "[reddit]  accounts: $NACC | posts: $(count "$POSTS") | newest-post: $(fresh "$POSTS") | live-check: $(liveurl "$POSTS")"
echo "          → PASS only if posts.jsonl grows AND newest comment URL is LIVE"
# 3 LM: improving (fresh pass + report) — revenue truth is Stripe (separate), no daily artifact to curl
LMHB="$HOME/.local/state/life-manager/state/.life-manager-loop-last-pass"
echo "[lm]      last-pass: $(fresh "$LMHB") | reports: $(grep -c 'loop=life-manager' "$HOME/.local/state/life-manager/logs/loop-report.log" 2>/dev/null||echo 0)"
echo "          → PASS only if a fresh pass + a real recorded funnel change (revenue via Stripe verify)"
# --- REQ-LV-040/104: Cadence Contract loops (clip/affiliate/video/gig/bounty/pm-earner/
# founder-loop, + clip-promote's own independent status). REPLACES the old fresh()/stale_hrs()
# artifact-age judgment for these 8 loops ONLY — the 3 blocks above (capafy/reddit/lm) keep
# fresh() unchanged (REQ-LV-104, out of this feature's scope). "did today's contracted cadence
# actually happen" (cadence.py, pure) rather than "is the artifact recent" (the old fresh() bug
# class G1 caught: a loop that ran once and then died could still read as healthy).
SELF_DIR="${VERIFY_LOOPS_SELF_DIR:-$LIFE_MANAGER_REPO/skills/self}"
cadence_line() {
  local loop="$1"
  python3 "$SELF_DIR/cadence-evidence.py" status "$loop" 2>/dev/null \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['scorecard'])" 2>/dev/null \
    || echo "❌missed (streak=0) [evidence-gather error]"
}
echo "[clip]        $(cadence_line clip)"
echo "[affiliate]   $(cadence_line affiliate)"
echo "[video]       $(cadence_line video)"
echo "[gig]         $(cadence_line gig)"
echo "[bounty]      $(cadence_line bounty)"
echo "[pm-earner]   $(cadence_line pm-earner)"
echo "[founder-loop] $(cadence_line founder-loop)"
# clip-promote: independent status (REQ-LV-120), NOT a Cadence Contract — no fixed posting
# cadence exists for campaign-dependent promote.fun payouts.
CLIP_PROMOTE_LEDGER="${EARN_LEDGER:-$HOME/.local/state/life-manager/state/clip-earn-ledger.jsonl}"
CLIP_PROMOTE_STATUS_MJS="${VERIFY_LOOPS_CLIP_PROMOTE_STATUS_MJS:-$LIFE_MANAGER_REPO/skills/earn/clip-promote/clip-promote-status.mjs}"
clip_promote_line() {
  TODAY_JST="$(TZ=Asia/Tokyo date +%F)" node --input-type=module -e "
import { clipPromoteStatus } from '$CLIP_PROMOTE_STATUS_MJS';
import { readFileSync, existsSync } from 'node:fs';
const path = '$CLIP_PROMOTE_LEDGER';
const rows = existsSync(path)
  ? readFileSync(path, 'utf8').split('\n').filter(Boolean).map(l => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean)
  : [];
const r = clipPromoteStatus(rows, process.env.TODAY_JST || '$(TZ=Asia/Tokyo date +%F)');
console.log(r.status === 'payout-today' ? '✅payout-today' : '❌no-payout-today');
" 2>/dev/null || echo "❌no-payout-today [status-check error]"
}
echo "[clip-promote] $(clip_promote_line)"

echo "--- self-fix result markers (autonomous fixes) ---"
# self-heal.md root cause #2: this used to list only 3 of the 10 loops self-fix.sh actually covers,
# so an honest FAIL diagnosis for e.g. affiliate (reCAPTCHA-blocked, #994) or bounty (sourcing
# exhausted, #995) was silently invisible in every report. All 10 Cadence Contract + non-cadence
# loops self-fix.sh drives are listed here now (never a subset — a marker file simply won't exist
# for a loop that hasn't been escalated yet, which is itself honest information).
for L in clip-loop affiliate-loop video-loop gig-loop bounty-loop pm-earner-loop founder-loop capafy-loop reddit-loop life-manager-loop; do r="$HOME/.local/state/life-manager/state/.self-fix-$L.result"; [ -f "$r" ] && echo "  [$L] $(cat "$r")"; done
echo "--- loop-report tail (real executions) ---"; tail -4 "$HOME/.local/state/life-manager/logs/loop-report.log" 2>/dev/null
