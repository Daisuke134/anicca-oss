#!/usr/bin/env bash
LIFE_MANAGER_REPO="${LIFE_MANAGER_REPO:-$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null)}"
[ -n "$LIFE_MANAGER_REPO" ] || { echo "LIFE_MANAGER_REPO could not be resolved" >&2; exit 2; }
export LIFE_MANAGER_REPO
# capafy-ig-marketing-daily.sh — B1-B4 IG line — DETERMINISTIC daily trigger for the Capafy
# Instagram marketing loop. Active IG account comes only from clip-accounts-capafy.json.
# launchd -> this script -> shared agent runner: provision-or-selector -> copy -> canonical video
# (B3) -> instagrapi post (B4) -> ledger -> report. Copy is agent judgment, NEVER hardcoded here.
# LaunchAgent: ai.anicca.capafy-ig-marketing-daily at 16:00 local; stdout/stderr use LOG below.
#
# ★ Fresh account: day 1-2 warmup ONLY. LIVE starts on day 3. Initial posts stay
#   NON-COMMERCIAL until the reach-health marker exists. ★
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"
set -uo pipefail
for ENV_FILE in "${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"; do
  [ -f "$ENV_FILE" ] || continue
  set -a; . "$ENV_FILE" 2>/dev/null; set +a
done
if [ -n "${LM_TELEGRAM_ALERT_CHAT_ID:-}" ] && [ -z "${TELEGRAM_ALERT_CHAT_ID:-}" ]; then
  export TELEGRAM_ALERT_CHAT_ID="$LM_TELEGRAM_ALERT_CHAT_ID"
fi
if [ -n "${LM_TELEGRAM_BOT_TOKEN:-}" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  export TELEGRAM_BOT_TOKEN="$LM_TELEGRAM_BOT_TOKEN"
fi
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHCTL_SAFE="${CAPAFY_LAUNCHCTL_SAFE:-$LIFE_MANAGER_REPO/bin/launchctl-safe}"
LAUNCHCTL_DOMAIN="${CAPAFY_LAUNCHCTL_DOMAIN:-gui/$(id -u)}"

selfheal_capafy_launchd() {
  local labels=(
    ai.anicca.capafy-goal-monitor
    ai.anicca.capafy-goal-monitor-hourly
    ai.anicca.capafy-goal-monitor-daily-close
    ai.anicca.capafy-loop-daily
    ai.anicca.capafy-loop-healthcheck
    ai.anicca.capafy-outcome-monitor
    ai.anicca.capafy-ig-account-manager
  )
  local label plist current newly_hourly=0
  for label in "${labels[@]}"; do
    case "$label" in
      ai.anicca.capafy-goal-monitor|ai.anicca.capafy-goal-monitor-hourly|ai.anicca.capafy-goal-monitor-daily-close|ai.anicca.capafy-loop-daily|ai.anicca.capafy-loop-healthcheck|ai.anicca.capafy-outcome-monitor|ai.anicca.capafy-ig-account-manager) ;;
      *) return 2 ;;
    esac
    plist="$HOME/Library/LaunchAgents/$label.plist"
    if current="$($LAUNCHCTL_SAFE print "$LAUNCHCTL_DOMAIN/$label" 2>/dev/null)"; then
      continue
    fi
    [ -f "$plist" ] || return 2
    "$LAUNCHCTL_SAFE" preflight >/dev/null || return $?
    "$LAUNCHCTL_SAFE" bootstrap "$LAUNCHCTL_DOMAIN" "$plist" >/dev/null || return $?
    "$LAUNCHCTL_SAFE" print "$LAUNCHCTL_DOMAIN/$label" >/dev/null 2>&1 || return 1
    [ "$label" = "ai.anicca.capafy-goal-monitor-hourly" ] && newly_hourly=1
  done
  if [ "$newly_hourly" -eq 1 ]; then
    "$LAUNCHCTL_SAFE" kickstart "$LAUNCHCTL_DOMAIN/ai.anicca.capafy-goal-monitor-hourly" >/dev/null || return $?
  fi
}

if [ "${CAPAFY_IG_MARKETING_SELFHEAL_TEST:-0}" = "1" ]; then
  selfheal_capafy_launchd
  exit $?
fi
if [ "${CAPAFY_HEADLESS_BRIDGE:-0}" = "1" ]; then
  :
elif ! selfheal_capafy_launchd; then
  echo "Capafy launchd self-heal failed — stopping marketing wake" >&2
  exit 2
fi
# shellcheck source=account_state.sh
. "$SCRIPT_DIR/account_state.sh"
MARKETING_ENGINE_DIR="$SCRIPT_DIR/../marketing-engine"
# shellcheck source=../marketing-engine/provision_prompt.sh
. "$MARKETING_ENGINE_DIR/provision_prompt.sh"
# shellcheck source=../marketing-engine/load_manifest.sh
. "$MARKETING_ENGINE_DIR/load_manifest.sh"
me_load_manifest "${MKT_MANIFEST:-capafy}" || true   # per-loop config — engine stays shared; set MKT_MANIFEST to run another loop on this same engine
INSTANCE="${MKT_INSTANCE:-capafy}"   # state namespace; capafy -> identical paths, other loops -> own
RUN_AGENT="$MARKETING_ENGINE_DIR/run_agent.sh"
LOG="$HOME/.local/state/life-manager/logs/${INSTANCE}-ig-marketing-daily.log"
ROT="$HOME/.local/state/life-manager/state/${INSTANCE}-marketing-rotation.jsonl"
ACCOUNTS_FILE="$(capafy_ig_accounts_file)"
if ! IG_HANDLE="$(resolve_capafy_ig_handle "$ACCOUNTS_FILE")"; then
  echo "account SSOT unreadable: handle resolution failed" >&2
  exit 2
fi
if ! IG_PORT="$(resolve_capafy_ig_port "$ACCOUNTS_FILE")"; then
  echo "account SSOT unreadable: port resolution failed" >&2
  exit 2
fi
if ! IG_STARTED_WARMING="$(resolve_capafy_ig_started_warming "$ACCOUNTS_FILE")"; then
  echo "account SSOT unreadable: warming timestamp resolution failed" >&2
  exit 2
fi
export CAPAFY_IG_HANDLE="$IG_HANDLE"
LANDING_URL="${MKT_BIO_LINK:-https://capafy-skills-daily.netlify.app}"
LANDING_SITE_ID="${MKT_LANDING_SITE_ID:-41c8e52e-b163-442a-84ff-fd866269bf6c}"
COOKED_MARKER="$HOME/.local/state/life-manager/state/.${INSTANCE}-ig-account-cooked"
COMMERCIAL_MARKER="$HOME/.local/state/life-manager/state/.${INSTANCE}-ig-reach-healthy"
export CAPAFY_IG_REACH_MARKER="$COMMERCIAL_MARKER"
PROVISION_REASON="$(capafy_ig_provision_reason "$IG_HANDLE" "$COOKED_MARKER")"
PROVISION_NEEDED="no"
[ -n "$PROVISION_REASON" ] && PROVISION_NEEDED="yes"
mkdir -p "$(dirname "$LOG")" "$(dirname "$ROT")"
# ── BROWSER ISOLATION (same lease system clip uses): take our own isolated context on :9222 so
#    capafy never churns against clip/gig/Dais tabs on the shared daily-driver (churn = poison). ──
BROWSER_SCRIPTS="$SCRIPT_DIR/../../browser/scripts"
CAPAFY_LEASE="capafy-$$"; export CAPAFY_LEASE
trap 'python3 "$BROWSER_SCRIPTS/cdp_context_lease.py" release "$CAPAFY_LEASE" >/dev/null 2>&1' EXIT
python3 "$BROWSER_SCRIPTS/cdp_context_lease.py" acquire "$CAPAFY_LEASE" >/dev/null 2>&1 || true
echo "=== capafy-ig-marketing-daily run $(date '+%F %T %Z') ===" >>"$LOG"
echo "account_state=$ACCOUNTS_FILE active_handle=${IG_HANDLE:-none} active_port=${IG_PORT:-none} provision_needed=$PROVISION_NEEDED reason=${PROVISION_REASON:-none}" >>"$LOG"
if [ "${CAPAFY_IG_PROBE_ONLY:-0}" = "1" ]; then
  printf 'active_handle=%s provision_needed=%s reason=%s\n' \
    "${IG_HANDLE:-none}" "$PROVISION_NEEDED" "${PROVISION_REASON:-none}"
  exit 0
fi

# ── IG metrics/attribution run EVERY day (deterministic; IG variants, utm_source=instagram_bio) ──
/opt/homebrew/bin/python3 $LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/ig_metrics.py >>"$LOG" 2>&1 || echo "ig_metrics failed (non-fatal)" >>"$LOG"
/opt/homebrew/bin/python3 $LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/pull_attribution.py >>"$LOG" 2>&1 || echo "pull_attribution failed (non-fatal)" >>"$LOG"

# ── All-skills bio landing refreshes on EVERY pass, including cadence no-op days. ──
# netlify-cli writes ./.netlify relative to cwd; launchd starts at / (no WorkingDirectory) -> mkdir '//.netlify' ENOENT. cd keeps it inside the skill dir.
LANDING_SITE="$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/site"
landing_fingerprint() {
  /opt/homebrew/bin/python3 - "$LANDING_SITE" <<'PY'
import hashlib, pathlib, sys
root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256()
for path in sorted(item for item in root.rglob("*") if item.is_file()):
    digest.update(str(path.relative_to(root)).encode())
    digest.update(path.read_bytes())
print(digest.hexdigest())
PY
}
LANDING_BEFORE="$(landing_fingerprint)"
if /opt/homebrew/bin/python3 "$LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/build_landing.py" >>"$LOG" 2>&1; then
  LANDING_AFTER="$(landing_fingerprint)"
  if [ "$LANDING_BEFORE" = "$LANDING_AFTER" ]; then
    echo "landing unchanged; deploy skipped" >>"$LOG"
  elif ! ( cd "$LIFE_MANAGER_REPO/skills/earn/capafy-marketing" && /opt/homebrew/bin/npx --yes netlify-cli@27.1.2 deploy --prod --dir "$LANDING_SITE" --site "$LANDING_SITE_ID" ) >>"$LOG" 2>&1; then
    echo "landing deploy failed (non-fatal)" >>"$LOG"
  fi
else
  echo "landing regenerate failed (non-fatal)" >>"$LOG"
fi

# ── WARMUP GATE: decide DRY vs LIVE. Creation date is day1; day1-2 DRY; LIVE from day3. ──
WARM_DAY="$(capafy_ig_warming_day "$IG_STARTED_WARMING")"
case "$WARM_DAY" in
  ''|*[!0-9]*) echo "warmup state unreadable — stopping pass" >>"$LOG"; exit 2 ;;
esac
if [ "$PROVISION_NEEDED" = "no" ] && [ "$WARM_DAY" -eq 0 ]; then
  echo "warmup state unreadable — existing account has day 0" >>"$LOG"
  exit 2
fi
# ★STRATEGY (2026-07-19 Dais, WHOLE marketing engine): warm up for 2 days, post from DAY 3.
# day1-2 = warmup ONLY (no posting) so the fresh account is NOT poisoned/cooled/polluted by
# early posting. instagrapi CAN post (proven) — the failure mode was posting too early, not the
# poster. From day3 the account is warm enough to post daily 100%. First live posts stay
# NON-COMMERCIAL (no bio link, pure-info caption) to measure reach before adding a commercial
# link. COMMERCIAL_OK only after the reach-check step writes the healthy marker.
MODE_FLAG=""   # empty = dry (build video+copy only, publish nothing). --live from day>=3.
LAST_PASS_MARKER="$HOME/.local/state/life-manager/state/.${INSTANCE}-ig-marketing-last-pass"
IG_LEDGER="$HOME/.local/state/life-manager/state/${INSTANCE}-marketing-ig-ledger.jsonl"
if [ "${WARM_DAY:-0}" -ge 3 ]; then MODE_FLAG="--live"; fi
COMMERCIAL_OK="no"
if /opt/homebrew/bin/python3 - "$COMMERCIAL_MARKER" "$IG_HANDLE" <<'PY'
import json, os, sys
try:
    row = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if row.get("status") == "reach_healthy" and row.get("handle") == sys.argv[2] else 1)
PY
then
  COMMERCIAL_OK="yes"
fi
echo "warmup day-count=$WARM_DAY -> post mode: ${MODE_FLAG:-DRY} | commercial_ok=$COMMERCIAL_OK" >>"$LOG"

# ── CADENCE GATE (rolling 20h, platform=ig) ──
CADENCE_RC=10
if [ "$PROVISION_NEEDED" = "no" ] && [ -e "$ROT" ] && [ ! -f "$ROT" ]; then
  echo "cadence state is not a regular file — stopping pass" >>"$LOG"
  exit 2
fi
if [ "$PROVISION_NEEDED" = "no" ] && [ -f "$ROT" ]; then
  /opt/homebrew/bin/python3 - "$ROT" <<'PY'
import json,sys,time
last=0
for line in open(sys.argv[1]):
    line=line.strip()
    if not line: continue
    r=json.loads(line)
    if r.get("platform")=="ig" and r.get("ts"): last=max(last,int(r["ts"]))
sys.exit(0 if last and (time.time()-last)<72000 else 10)
PY
  CADENCE_RC=$?
fi
if [ "$CADENCE_RC" -eq 0 ]; then
  echo "cadence gate: last IG Reel < 20h ago — no-op." >>"$LOG"
  touch "$LAST_PASS_MARKER" || exit 2
  exit 0
fi
[ "$CADENCE_RC" -eq 10 ] || { echo "cadence state unreadable — stopping pass" >>"$LOG"; exit 2; }

# Select exactly once, read-only. Rotation is committed only after a new native
# Reel row for this exact Agent is verified below.
SELECTED_JSON='{}'
SELECTED_AGENT_ID=''
CAMPAIGN_URL=''
PRE_IG_ROWS=0
CREATIVE_APPROVAL_STATUS='none'
APPROVED_ARTIFACT_PATH=''
APPROVED_ARTIFACT_SHA256=''
CREATIVE_APPROVAL_FILE="$HOME/.local/state/life-manager/state/${INSTANCE}-creative-approval.json"
if [ "$PROVISION_NEEDED" = "no" ]; then
  SELECTED_JSON="$(/opt/homebrew/bin/python3 "$SCRIPT_DIR/scripts/select_listing.py")" || {
    echo "evidence-ready listing selection failed: $SELECTED_JSON" >>"$LOG"
    exit 1
  }
  SELECTED_AGENT_ID="$(printf '%s' "$SELECTED_JSON" | /opt/homebrew/bin/python3 -c 'import json,sys; print(json.load(sys.stdin).get("agent_id") or "")')"
  [ -n "$SELECTED_AGENT_ID" ] || exit 1
  CAMPAIGN_URL="${LANDING_URL%/}/go/${SELECTED_AGENT_ID}"
  [ -f "$IG_LEDGER" ] && PRE_IG_ROWS="$(wc -l < "$IG_LEDGER" | tr -d ' ')"
  APPROVAL_JSON="$(/opt/homebrew/bin/python3 "$SCRIPT_DIR/scripts/creative_approval.py" \
    --state "$CREATIVE_APPROVAL_FILE" \
    --agent-id "$SELECTED_AGENT_ID" \
    --artifact-root "$HOME/.local/state/life-manager/artifacts/capafy/ig")" || {
      echo "creative approval state invalid — stopping pass: $APPROVAL_JSON" >>"$LOG"
      exit 2
    }
  CREATIVE_APPROVAL_STATUS="$(printf '%s' "$APPROVAL_JSON" | /opt/homebrew/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
  if [ "$CREATIVE_APPROVAL_STATUS" = "pending" ]; then
    echo "HEALTHY-IDLE: selected Agent $SELECTED_AGENT_ID creative is pending user approval; platform write=0" >>"$LOG"
    touch "$LAST_PASS_MARKER" || exit 2
    exit 0
  fi
  if [ "$CREATIVE_APPROVAL_STATUS" = "approved" ]; then
    APPROVED_ARTIFACT_PATH="$(printf '%s' "$APPROVAL_JSON" | /opt/homebrew/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["artifact_path"])')"
    APPROVED_ARTIFACT_SHA256="$(printf '%s' "$APPROVAL_JSON" | /opt/homebrew/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["artifact_sha256"])')"
  fi
fi

PROVISION_PROMPT="$(
  IG_PROVISION_ACCOUNT_STATE_FILE="$ACCOUNTS_FILE" \
  IG_PROVISION_HANDLE_PREFIX="${MKT_HANDLE_PREFIX:-capafy}" \
  IG_PROVISION_INSTANCE="${MKT_INSTANCE:-capafy}" \
  IG_PROVISION_GMAIL_PLUS_TAG_PREFIX="${MKT_GMAIL_PLUS_TAG_PREFIX:-capafy}" \
  IG_PROVISION_BIO_TEXT="${MKT_BIO_TEXT:-one-line Claude-skills bio, NO link}" \
  IG_PROVISION_PORT="9332" \
  IG_PROVISION_CONTEXT_ID="$CAPAFY_LEASE" \
  IG_PROVISION_BROWSER_INSTRUCTIONS="Launch and use the dedicated CloakBrowser profile on :9332 for context '$CAPAFY_LEASE'. Never use or log in through the raw shared :9222 browser." \
  IG_PROVISION_PROFILE_PREFIX="${MKT_PROFILE_PREFIX:-capafy-mkt}" \
  IG_PROVISION_COOKED_MARKER="$COOKED_MARKER" \
  IG_PROVISION_REASON="${PROVISION_REASON:-none}" \
  IG_PROVISION_REACH_MARKER="$COMMERCIAL_MARKER" \
  IG_PROVISION_LAST_PASS_MARKER="$LAST_PASS_MARKER" \
  IG_PROVISION_TELEGRAM_TARGET="${CAPAFY_TELEGRAM_TARGET:-${TELEGRAM_ALERT_CHAT_ID:?CAPAFY_TELEGRAM_TARGET or TELEGRAM_ALERT_CHAT_ID is required}}" \
  render_ig_provision_prompt
)"

PROMPT='You are the Anicca Capafy IG-marketing loop (headless; goal = drive Capafy skill subscribers via Instagram Reels; revenue → Dais bank; human NOT in loop). Triggered by launchd (ai.anicca.capafy-ig-marketing-daily). Active account SSOT is '"$ACCOUNTS_FILE"'; resolved handle='"${IG_HANDLE:-none}"', port='"${IG_PORT:-none}"'. NEVER use a Dais-personal account. The bash caller passed MODE='"${MODE_FLAG:-DRY}"'. If MODE=DRY, do not publish. Only MODE=--live may publish.

PROVISION GATE: provision_needed='"$PROVISION_NEEDED"', reason='"${PROVISION_REASON:-none}"'. When provision_needed=yes, run STEP PROVISION below and STOP THIS PASS after its success/failure report. Do not run STEP1-STEP7, do not build content, do not edit bio, and do not publish. A newly provisioned account is day 1 and MUST receive zero posts until warmup reaches day 3.

'"$PROVISION_PROMPT"'

STEP1 SELECTED (deterministic caller; do not call selector again): '"$SELECTED_JSON"'. Use exactly this Agent and its evidence_source. Never advance rotation during exploration.

CREATIVE APPROVAL: status='"$CREATIVE_APPROVAL_STATUS"', approved_artifact='"$APPROVED_ARTIFACT_PATH"', approved_sha256='"$APPROVED_ARTIFACT_SHA256"'. When status=approved, skip new creative generation and use only that exact artifact after recomputing and matching its SHA-256. Never substitute, modify, remux, or re-render approved bytes. A mismatch is terminal before POST.

COMMERCIAL GATE: commercial_ok='"$COMMERCIAL_OK"'. While commercial_ok=no, EVERY post is NON-COMMERCIAL: pure-info caption ("here is a Claude skill that does X" — NO "buy/subscribe/link in bio" push), and DO NOT add any Capafy link to the bio yet. This avoids the day-0 commercial-link suspension trigger while we measure reach. Only when commercial_ok=yes do you add the bio link + a soft CTA.

STEP2 COPY (YOUR judgment, no template): from name+desc write (a) a Reel caption (hook + what the skill does; if commercial_ok=yes include the exact campaign URL '"$CAMPAIGN_URL"' as its own final line plus a soft CTA, even though Instagram may render caption URLs as non-clickable text; else pure info, NO push and NO URL) and (b) a one-line on-screen hook for the video. The campaign URL must match the selected Agent and makes every video description self-identifying; never substitute the generic all-skills homepage. Before writing, if $LIFE_MANAGER_REPO/skills/earn/capafy-marketing/IG_BEST_PRACTICES.md exists, read it and follow its measured winning patterns; if absent, use your normal judgment.

STEP3 VIDEO (B3, APPROVED HyperFrames V4 contract): when CREATIVE APPROVAL status is approved, use the exact approved artifact above and do not render. Otherwise create one unique run directory below $HOME/.local/state/life-manager/artifacts/capafy/ig/. Find a repo-owned test fixture or immutable live output receipt for THIS selected listing; do not invent a result from its description. If no source exists, fail before rendering/posting. Use `$LIFE_MANAGER_REPO/skills/video/hyperframes/capafy-o13-review/` only as the approved visual/technical reference; copy its HyperFrames 0.8.8 project shape into the run directory and author four listing-specific 1080x1920 scenes that visibly match the narration: pain/raw input -> source evidence -> transformation -> verified output/CTA. Do not reuse O13 text for another listing. Generate four separate scene narration clips with free `edge-tts --voice en-US-AndrewNeural`, constrain each clip to its matching visual scene window, join them with zero scene-boundary crossings, and normalize the final mix near -16 LUFS. The rejected Samantha and Indian-accent Mona voices are forbidden. Run pinned `npx --yes hyperframes@0.8.8 render` as one foreground exec session. Shell `&`, `nohup`, detached subprocesses, `kill -0`, `ps`, and custom polling loops are forbidden. If the tool returns a running session ID, call `write_stdin` on that exact session until it returns the process exit code; do not launch a second render and do not inspect any MP4 while the session is running. Continue only after that same render session exits 0 and the MP4 size and SHA-256 are identical across two probes at least 2 seconds apart. The old canonical-renderer and generic text-card fallback are forbidden. Gate before POST: HyperFrames check/lint passes; final MP4 is 1080x1920, about 30 seconds, H.264/AAC; four scene files are present; the source path/hash, selected Agent ID, scene timings, voice, output hash and inspection frames are saved in a manifest; inspect full-resolution frames from all four scenes and reject blank rectangles, tiny text, mismatched narration/content, generic b-roll, or reused O13 copy. A render, inspection, evidence, or audio-sync failure is terminal; never post a fallback.

STEP4 POST (B4, shared instagrapi poster): CDP_PORT='"$IG_PORT"' /opt/homebrew/bin/python3 $LIFE_MANAGER_REPO/skills/earn/marketing-engine/poster.py --video <mp4> --caption-file <caption> --handle '"$IG_HANDLE"' --port '"$IG_PORT"' --accounts-path '"$ACCOUNTS_FILE"' --live . The poster must try ~/.cloak/instagrapi-'"$IG_HANDLE"'.json as tier1 and verify the authenticated handle. Do not reject a valid tier1 session merely because the account lifecycle SSOT remains session_owner=browser; that state permits the existing tier2 browser-session fallback only after tier1 is unavailable. Only run when MODE=--live; if MODE=DRY do not post. If it returns ChallengeRequired, stop and report; never retry-login. Capture post_url.

STEP5 BIO (deterministic — do NOT hand-drive the profile UI): set the profile Website to the selected Agent campaign URL '"$CAMPAIGN_URL"' ONLY when commercial_ok=yes AND MODE=--live. This URL records the click and immediately redirects to the selected Agent listing on Capafy; it never shows the generic all-skills page. Use the repo-owned persistence-verifying script: open the account edit page in THIS pass isolated lease context and run  python3 $LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/setup_profile.py --tid <the lease tab TID for '"$IG_HANDLE"'> --website '"$CAMPAIGN_URL"' --username '"$IG_HANDLE"'  . It returns website_set=true only if IG kept the FULL link; if website_set=false, IG stripped it — report that and do NOT claim the bio link is installed. While commercial_ok=no, DO NOT touch the bio. Never in DRY.

STEP6 VERIFY + LEDGER + REACH: on --live, confirm the Reel is publicly visible and append exactly one row to '"$IG_LEDGER"' containing platform=ig, reel_url, agent_id, listing_name, handle, artifact_sha256, plus the exact nonempty `caption` and nonempty on-screen `hook` used in this post. These copy fields are mandatory experiment evidence, not optional prose. Record post time in the rotation ledger (platform=ig). Then MEASURE REACH (the real shadowban test): run  python3 $LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/ig_metrics.py  to snapshot views/likes/comments, and (a few hours after a post, or on the NEXT day pass) judge: is reach healthy for a fresh account (getting non-zero views/plays, appearing when you search its own hashtags)? If reach looks HEALTHY on the accumulated snapshots, write the marker  touch '"$COMMERCIAL_MARKER"'  (this flips commercial_ok=yes → next posts add the bio link + soft CTA). If reach looks SHADOWBANNED (near-zero views across multiple posts, not in hashtag/explore), do NOT write the marker — instead report it so a human/next pass decides account-rebuild vs warmup-extend. Never fabricate reach numbers. On DRY, just record the flow reached share cleanly.
After REACH, run python3 $LIFE_MANAGER_REPO/skills/earn/capafy-marketing/scripts/ig_reflect.py exactly once to refresh IG_BEST_PRACTICES.md from real ledger + metrics data for the next pass.

STEP7 REPORT — MANDATORY every pass. Send to the Telegram target in CAPAFY_TELEGRAM_TARGET or TELEGRAM_ALERT_CHAT_ID via openclaw message send:
  (a) the VIDEO itself as media: openclaw message send --channel telegram --target "\${CAPAFY_TELEGRAM_TARGET:-\$TELEGRAM_ALERT_CHAT_ID}" --media <the mp4 path> --force-document --message "<caption below>" --json  (--force-document keeps it uncompressed; if the video attach fails, fall back to sending a thumbnail/first-frame png + the message).
  (b) the message body MUST contain: the promoted listing name + agent_id, the mode (DRY or LIVE), the Reel public URL (or "DRY — not posted" on a dry pass), and the FULL caption text verbatim (the exact caption you wrote for the Reel).
  On a DRY pass you STILL send this once (video + full caption + which listing) so Dais can review the creative before go-live — you just do NOT publish to IG. Confirm the send returned a real message id; also AgentMail via loop-report if that path exists.

Do not write '"$LAST_PASS_MARKER"'; the deterministic wrapper owns that heartbeat and writes it only after this runner exits 0. A DRY pass or a deferred cadence pass is a clean finish.'

EVIDENCE_DIR="$HOME/.local/state/life-manager/state/agent-runner-evidence/${INSTANCE}-ig-marketing/$(date +%s)-$$"
printf '%s\n' "$PROMPT" | AGENT_RUNNER_EVIDENCE_MIN_FREE_BYTES=67108864 "$RUN_AGENT" \
  --task-class marketing-agent \
  --evidence-dir "$EVIDENCE_DIR" \
  --task-label "${INSTANCE}-ig-marketing-daily" \
  --loop capafy >>"$LOG" 2>&1
RC=$?
if [ "$RC" -eq 0 ] && [ "$PROVISION_NEEDED" = "no" ] && [ "$MODE_FLAG" = "--live" ]; then
  VERIFIED_POST="$(/opt/homebrew/bin/python3 - "$IG_LEDGER" "$PRE_IG_ROWS" "$SELECTED_AGENT_ID" <<'PY'
import json, pathlib, sys
path, before, agent_id = pathlib.Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
rows = path.read_text().splitlines()[before:] if path.is_file() else []
for line in rows:
    try:
        row = json.loads(line)
    except Exception:
        continue
    if (
        str(row.get("agent_id")) == agent_id
        and str(row.get("reel_url") or "").startswith("https://www.instagram.com/reel/")
        and all(str(row.get(key) or "").strip() for key in ("listing_name", "caption", "hook"))
    ):
        print(row["reel_url"])
        raise SystemExit
print("")
PY
)"
  if [ -z "$VERIFIED_POST" ]; then
    echo "live pass produced no verified native Reel for selected Agent $SELECTED_AGENT_ID" >>"$LOG"
    RC=3
  else
    /opt/homebrew/bin/python3 "$SCRIPT_DIR/scripts/select_listing.py" --commit-agent-id "$SELECTED_AGENT_ID" >>"$LOG" 2>&1 || RC=4
  fi
fi
echo "=== capafy-ig-marketing-daily done rc=$RC $(date '+%F %T %Z') ===" >>"$LOG"
[ "$RC" -eq 0 ] || exit "$RC"
touch "$LAST_PASS_MARKER" || exit 2
exit 0
