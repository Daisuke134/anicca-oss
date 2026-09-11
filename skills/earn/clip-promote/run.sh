#!/usr/bin/env bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# earn/clip-promote — promote.fun USDC-Solana per-view clipping SLOT entrypoint (run-skill.mjs spawns
# this each wake). Does EXACTLY ONE bounded state-machine transition per wake (idempotent), prints ONE
# structured JSON line on stdout, exit 0. NO HUMAN: captcha→CapSolver, OTP→gog gmail, login→stored creds.
#
# Slot contract (REQ-12): one bounded transition chosen by the PURE decide(state,now); ONE line out.
# Watchdog (REQ-9): every browser/IO step is wrapped in a portable timeout; if a step blocks on human
# input it trips → did:"blocked:human:<step>" exit 0 (a recorded defect, NEVER a hang).
# DONE (REQ-8): the RECORD transition is the ONLY one that prints earned_usdc>0, via record-payout.mjs
# run under `env -i` (no PII) so the malice-guard passes.
set -uo pipefail

# The browser is shared with the other money loops: heal it, restore the logins, collect stray tabs.
bash "$LIFE_MANAGER_REPO/skills/browser/ensure_browser.sh" || echo "WARN: browser not recovered"

SK="$LIFE_MANAGER_REPO/skills/earn/clip-promote"
PY=/opt/homebrew/bin/python3
NODE=/opt/homebrew/bin/node
[ -x "$NODE" ] || NODE=node
[ -x "$PY" ] || PY=python3
# SHARED-1 (INV-5): posting reads go through poster.py (earn/marketing-engine), the free
# instagrapi-based poster -- shares the same self-healed venv as earn/clip.
INSTA_VENV="$HOME/.cache/instagrapi-venv"
INSTA_PY="$INSTA_VENV/bin/python"
INSTA_POSTER="$LIFE_MANAGER_REPO/skills/earn/marketing-engine/poster.py"

# env (own-identity only; PII is NOT scrubbed by the harness — we keep it out of the RECORD subprocess).
set -a; . "$HOME/.local/state/life-manager/.env" 2>/dev/null || true; set +a

EARN_MODE="${EARN_MODE:-discover}"
WAKE="${WAKE_ID:-$(date -u +%s)}"
STEP_DEADLINE_S="${STEP_DEADLINE_S:-120}"
SOLANA_RPC_URL="${SOLANA_RPC_URL:-https://api.mainnet-beta.solana.com}"
WALLET="${CLIP_WALLET_SOLANA:-xxKC33TYJ2czjGQAADrvDCLjF6pRvtHX125fCwP5u9H}"
STATE="${CLIP_PROMOTE_STATE:-$HOME/.cloak/clip-promote-state.json}"
LEDGER="${EARN_LEDGER:-$HOME/.local/state/life-manager/state/clip-earn-ledger.jsonl}"
ACCTS="${CLIP_ACCOUNTS:-$HOME/.cloak/clip-accounts.json}"
CDP_DIR="${CDP_DIR:-$HOME/.claude/skills/ig-account-create/scripts}"
mkdir -p "$(dirname "$STATE")" "$(dirname "$LEDGER")" 2>/dev/null || true

# shared emit / run_step (portable watchdog, FIND-302) / blocked_or (fail-closed) — see _lib.sh.
. "$SK/_lib.sh"

# load the current pipeline state (missing ⇒ {} ⇒ decide returns SELECT).
read_state() { [ -f "$STATE" ] && cat "$STATE" || echo '{}'; }
NOW="$(date -u +%s)"
TRANS="$("$PY" -c "import json,sys;sys.path.insert(0,'$SK');from decide import decide;print(decide(json.loads(sys.stdin.read() or '{}'),$NOW))" <<<"$(read_state)" 2>/dev/null)"
[ -z "$TRANS" ] && TRANS="SELECT"   # decide failure ⇒ safe SELECT, never a dead-end

# discover mode: report the transition this wake WOULD run; take NO side effect (not a fake run — it
# claims no earn/post; it is how the loop inspects state).
if [ "$EARN_MODE" != "execute" ]; then
  emit "discover: would run transition=$TRANS (state=$STATE)"; exit 0
fi

# ── execute mode: run exactly the ONE transition. The live browser/promote.fun steps (SELECT/CLIP/POST/
#    SUBMIT/WITHDRAW) are wired in #14 against real infra; each is wrapped in run_step so a human-blocking
#    step trips the watchdog. RECORD is fully implemented (the DONE gate). ──
case "$TRANS" in
  RECORD)
    SIG="$("$PY" -c "import json;print((json.load(open('$STATE')) if __import__('os').path.exists('$STATE') else {}).get('sig',''))" 2>/dev/null)"
    if [ -z "$SIG" ]; then emit "record:no-sig"; exit 0; fi
    # env -i (FIND-301): the RECORD subprocess gets ONLY public wallet/RPC/ledger — no PII var reaches
    # the malice-guard in record.mjs.
    RES="$(run_step "$STEP_DEADLINE_S" env -i PATH="$PATH" HOME="$HOME" \
            SOLANA_RPC_URL="$SOLANA_RPC_URL" SIG="$SIG" WALLET="$WALLET" \
            EARN_LEDGER="$LEDGER" WAKE_ID="$WAKE" \
            "$NODE" "$SK/record-payout.mjs" 2>/dev/null)"; rc=$?
    blocked_or "record" "$rc" "record:step-failed"
    EARNED="$("$PY" -c "import json,sys;d=json.loads(sys.argv[1] or '{}');print(d.get('earn_usdc',0) if d.get('status')=='recorded' else 0)" "$RES" 2>/dev/null)"
    STATUS="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1] or '{}').get('status','?'))" "$RES" 2>/dev/null)"
    if [ "$STATUS" = "recorded" ]; then
      # DONE: free the slot for the next campaign on the next wake.
      "$PY" -c "import json;json.dump({'phase':'idle'},open('$STATE','w'))" 2>/dev/null || true
      emit "record:DONE sig=$SIG" "${EARNED:-0}" 0; exit 0
    fi
    emit "record:$STATUS"; exit 0
    ;;
  SELECT)
    # REQ-1: fetch ACTIVE campaigns from the logged-in promote.fun tab + rank by cpm*budget; pick the top
    # not-yet-clipped, advance state to CLIP. REQ-1a: none eligible → narrate idle.
    PF_PORT="${CLIP_PROMOTE_CDP_PORT:-9222}"
    PF_TID="$(find_promote_tab "$PF_PORT")"
    if [ -z "$PF_TID" ]; then emit "select:no-promote-tab (login session not open on :$PF_PORT)"; exit 0; fi
    CLIPPED="$("$PY" -c "import json,os;p='$STATE';d=json.load(open(p)) if os.path.exists(p) else {};print(','.join(d.get('clipped',[])))" 2>/dev/null)"
    RANKED="$(run_step "$STEP_DEADLINE_S" env CDP_DIR="$CDP_DIR" PF_TID="$PF_TID" CDP_PORT="$PF_PORT" \
              "$PY" "$SK/select_campaigns.py" 2>/dev/null)"; rc=$?
    blocked_or "select" "$rc" "select:fetch-failed"
    PICK="$("$PY" -c "import json,sys
r=json.loads(sys.argv[1] or '[]'); clipped=set('$CLIPPED'.split(',')) if '$CLIPPED' else set()
r=[c for c in r if c['slug'] not in clipped]
print(json.dumps(r[0]) if r else '')" "$RANKED" 2>/dev/null)"
    if [ -z "$PICK" ]; then emit "no-eligible-campaign"; exit 0; fi
    SLUG="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1])['slug'])" "$PICK")"
    "$PY" -c "import json,os
p='$STATE'; d=json.load(open(p)) if os.path.exists(p) else {}
pick=json.loads('''$PICK'''); d.update({'phase':'JOIN','campaign_id':pick['slug'],'cpm':pick['cpm'],'budget':pick['budget']})
json.dump(d,open(p,'w'))" 2>/dev/null
    emit "select:$SLUG (cpm=$("$PY" -c "import json,sys;print(json.loads(sys.argv[1])['cpm'])" "$PICK")), next=JOIN"; exit 0
    ;;
  JOIN)
    # REQ-1b: a campaign's minimum-follower requirement is invisible until join is attempted
    # (Rules tab locked pre-join, verified live 2026-07-04). If join fails on a follower-count
    # gate, record the campaign as permanently skipped (added to 'clipped' so SELECT never
    # re-picks it) and free the slot back to SELECT -- this is NOT a fabricated failure, it is
    # the honest "we don't yet have enough followers/trust for this one" outcome (Dais
    # 2026-07-04: "in order to do good money-making affiliate work, you have to earn trust and
    # followers first" -- the parallel earn/clip posting loop on the SAME account is what
    # organically grows that follower count over time; this loop just keeps politely deferring
    # until it does).
    SLUG="$("$PY" -c "import json;print(json.load(open('$STATE')).get('campaign_id',''))" 2>/dev/null)"
    [ -n "$SLUG" ] || { emit "join:no-campaign-id-in-state"; exit 0; }
    PF_PORT="${CLIP_PROMOTE_CDP_PORT:-9222}"
    PF_TID="$(find_promote_tab "$PF_PORT")"
    [ -n "$PF_TID" ] || { emit "join:no-promote-tab (login session not open on :$PF_PORT)"; exit 0; }
    JOIN_RES="$(run_step "$STEP_DEADLINE_S" env CDP_DIR="$CDP_DIR" PF_TID="$PF_TID" CDP_PORT="$PF_PORT" \
              "$PY" "$SK/join_campaign.py" "$SLUG" 2>/dev/null)"; rc=$?
    blocked_or "join" "$rc" "join:attempt-failed"
    JOINED="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1] or '{}').get('joined', False))" "$JOIN_RES" 2>/dev/null)"
    REASON="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1] or '{}').get('reason', '?'))" "$JOIN_RES" 2>/dev/null)"
    if [ "$JOINED" = "True" ]; then
      "$PY" -c "import json,os
p='$STATE'; d=json.load(open(p)) if os.path.exists(p) else {}
d.update({'phase':'CLIP'})
json.dump(d,open(p,'w'))" 2>/dev/null
      emit "join:ok-for-$SLUG (next=CLIP)"; exit 0
    fi
    # join failed (ended / follower gate / button missing) -- mark permanently skipped, free the slot
    "$PY" -c "import json,os
p='$STATE'; d=json.load(open(p)) if os.path.exists(p) else {}
clipped=set(d.get('clipped', [])); clipped.add('$SLUG')
d.update({'phase':'idle','clipped':sorted(clipped)})
json.dump(d,open(p,'w'))" 2>/dev/null
    emit "join:skipped-$SLUG-$REASON (permanently excluded, freed slot for next SELECT)"; exit 0
    ;;
  CLIP)
    # REQ-2/REQ-3: extract the selected campaign's source video URL (deterministic: first
    # youtube.com/youtu.be link on its detail page -- verified 2026-07-04 this works for
    # campaigns whose provided content IS a YouTube video, e.g. thomas-rhett-content's
    # "Podcast 1/2" links; campaigns that only offer a Google Drive folder, e.g. crocs,
    # return no URL and are honestly reported as not-auto-clippable rather than faked).
    # Then reuse the EXISTING earn/clip producer.sh (yt-dlp+whisper+crop, proven engine) to
    # produce a queued clip under a SEPARATE clip-promote instance queue (ANICCA_INSTANCE=
    # clip-promote => ~/clips/queue-clip-promote, never mixes with the earn/clip loop's own
    # queue). NOTE: producer.sh's own caption template (generic clip-loop hashtags) does NOT
    # satisfy THIS campaign's required tags/CTA -- that override happens in POST (not yet
    # wired), which is the step that actually publishes.
    SLUG="$("$PY" -c "import json;print(json.load(open('$STATE')).get('campaign_id',''))" 2>/dev/null)"
    [ -n "$SLUG" ] || { emit "clip:no-campaign-id-in-state"; exit 0; }
    PF_PORT="${CLIP_PROMOTE_CDP_PORT:-9222}"
    PF_TID="$(find_promote_tab "$PF_PORT")"
    [ -n "$PF_TID" ] || { emit "clip:no-promote-tab (login session not open on :$PF_PORT)"; exit 0; }
    SRC_URL="$(run_step "$STEP_DEADLINE_S" env CDP_DIR="$CDP_DIR" PF_TID="$PF_TID" CDP_PORT="$PF_PORT" \
              "$PY" "$SK/fetch_source_video.py" "$SLUG" 2>/dev/null)"; rc=$?
    blocked_or "clip-fetch-source" "$rc" "clip:fetch-source-failed"
    if [ -z "$SRC_URL" ]; then
      emit "clip:no-usable-source-video (campaign $SLUG has no auto-extractable YouTube link)"; exit 0
    fi
    PRODUCER_RC=0
    ANICCA_INSTANCE=clip-promote run_step "$STEP_DEADLINE_S" bash "$LIFE_MANAGER_REPO/skills/earn/clip/producer.sh" --url "$SRC_URL" >/tmp/clip-promote-producer.log 2>&1 || PRODUCER_RC=$?
    blocked_or "clip-produce" "$PRODUCER_RC" "clip:produce-failed (see /tmp/clip-promote-producer.log)"
    if ! ls "$HOME/clips/queue-clip-promote"/*.mp4 >/dev/null 2>&1; then
      emit "clip:producer-ran-but-no-clip-in-queue (see /tmp/clip-promote-producer.log)"; exit 0
    fi
    "$PY" -c "import json,os
p='$STATE'; d=json.load(open(p)) if os.path.exists(p) else {}
d.update({'phase':'POST'})
json.dump(d,open(p,'w'))" 2>/dev/null
    emit "clip:produced-for-$SLUG (queued in ~/clips/queue-clip-promote, ready for POST)"; exit 0
    ;;
  POST)
    # REQ-4: post via poster.py (earn/marketing-engine) to a WARMED account only (~/.cloak/clip-accounts.json
    # status=="ready"). Currently only @aiclipsvault (port 9223) is ready -- the SAME account
    # the earn/clip loop itself posts to. KNOWN RISK (not yet mitigated): two independent
    # loops (earn/clip hourly, earn/clip-promote every 2h) could in principle race for the
    # same browser tab; the schedules make actual collision unlikely but not impossible --
    # revisit if collisions are observed in practice.
    #
    # CAPTION GAP (deliberate, safety-first): this cut posts the clip's own GENERIC caption
    # as-is. Campaign-specific required tags/CTA (e.g. thomas-rhett-content's mandatory
    # "@humanschool @milesadcox" / CTA line) are NOT yet applied -- each campaign's Rules tab
    # wording differs enough that a hardcoded per-campaign template is the wrong shape
    # (judgment call, better suited to an agent reading the Rules tab than a bash case
    # statement); deferred as a follow-up. Because publishing content that fails a
    # campaign's stated requirements risks the submission being rejected or the account
    # being penalized, THIS STEP STAYS --dry (verify-only, discards, never actually posts)
    # until the caption gap is closed. Do not flip to --live before that.
    ACCT_JSON="${CLIP_ACCOUNTS:-$HOME/.cloak/clip-accounts.json}"
    READY="$("$PY" -c "import json;d=json.load(open('$ACCT_JSON'));r=[a for a in d if a.get('status')=='ready'];print(json.dumps(r[0]) if r else '')" 2>/dev/null)"
    [ -n "$READY" ] || { emit "post:no-warm-account"; exit 0; }
    HANDLE="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1])['handle'])" "$READY" 2>/dev/null)"
    ACCT_PORT="$("$PY" -c "import json,sys;print(json.loads(sys.argv[1])['port'])" "$READY" 2>/dev/null)"
    CLIP_FILE="$(ls "$HOME/clips/queue-clip-promote"/*.mp4 2>/dev/null | head -1)"
    [ -n "$CLIP_FILE" ] || { emit "post:no-clip-in-queue"; exit 0; }
    CAP_FILE="${CLIP_FILE%.mp4}.txt"
    [ -f "$CAP_FILE" ] || CAP_FILE="/dev/null"
    if [ ! -x "$INSTA_PY" ]; then
      /opt/homebrew/bin/python3 -m venv "$INSTA_VENV" >/tmp/clip-promote-insta-venv.log 2>&1
      "$INSTA_PY" -m pip install --quiet instagrapi websocket-client pillow >/tmp/clip-promote-insta-pip.log 2>&1 || true
    fi
    # poster.py has NO --dry flag -- dry is its DEFAULT (omitting --live IS the dry-run):
    # it logs in and stops (outcome="dry") before ever touching clip_upload (SHARED-1 INV-5:
    # a login-only check, replacing the prior web-composer dry-verify).
    RES="$(CDP_PORT="$ACCT_PORT" run_step "$STEP_DEADLINE_S" "$INSTA_PY" "$INSTA_POSTER" --video "$CLIP_FILE" --caption-file "$CAP_FILE" --handle "$HANDLE" --port "$ACCT_PORT" 2>/dev/null | tail -1)"; rc=$?
    blocked_or "post-dry-verify" "$rc" "post:dry-verify-failed"
    emit "post:dry-verify-ok-for-$HANDLE-campaign-tags-not-yet-applied-staying-dry: $RES"
    exit 0
    ;;
  SUBMIT|WITHDRAW|STALLED)
    # Wired in subsequent increments (submit the live post URL to the campaign / withdraw),
    # each under run_step. Until wired, narrate honestly — NO fake success.
    emit "execute:$TRANS:not-yet-wired"; exit 0
    ;;
  *)
    emit "unknown-transition:$TRANS"; exit 0
    ;;
esac
