#!/usr/bin/env bash
# publish-note.sh — publish article to note.com via note-mcp Python lib
# Auth: NOTE_EMAIL / NOTE_PASSWORD from LIFE_MANAGER_ENV_FILE → browser session
# → cookie. Task #31 (2026-07-16): the actual credentials used to live here in plaintext
# (both in this comment and as a hardcoded default two lines below) -- removed. They still
# exist in this file's git history until rotated; rotation is a Dais decision, not done here.
#
# Usage:
#   bash publish-note.sh --markdown-file <f> --title <t> [--description <d>] [--tags "AI,agent,..."]
# stdout: release URL on success
set -euo pipefail

MD_FILE=""; TITLE=""; DESCRIPTION=""; TAGS_RAW=""; DRAFT_KEY_ARG=""; NEW_DRAFT_FLAG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --markdown-file) MD_FILE="$2"; shift 2 ;;
    --title)         TITLE="$2"; shift 2 ;;
    --description|--meta) DESCRIPTION="$2"; shift 2 ;;
    --tags)          TAGS_RAW="$2"; shift 2 ;;
    # --key: explicit draft key to update instead of creating a new draft (overrides the ledger).
    --key)           DRAFT_KEY_ARG="$2"; shift 2 ;;
    # --new-draft: escape hatch — ignore both --key and the ledger, always create a fresh draft.
    --new-draft)     NEW_DRAFT_FLAG=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
[[ -f "$MD_FILE" && -n "$TITLE" ]] || { echo "FATAL: --markdown-file --title required" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=writer-runtime-env.sh
source "$SCRIPT_DIR/writer-runtime-env.sh"

# --- fail-closed PII gate (scripts/pii-gate.py) ---------------------------------------
# Nothing operator-identifying may reach note.com. ANY non-zero exit from the gate -- a finding,
# an unconfigured blocklist, or an internal scanner error -- aborts this publish. Gate output
# goes to stderr so it cannot pollute this script's single-line stdout contract.
python3 "$SCRIPT_DIR/pii-gate.py" --stage publish-note "$MD_FILE" >&2 || exit $?


[[ -n "${NOTE_EMAIL:-}" && -n "${NOTE_PASSWORD:-}" ]] || { echo "FATAL: NOTE_EMAIL / NOTE_PASSWORD missing from LIFE_MANAGER_ENV_FILE" >&2; exit 1; }
# note-mcp's login_with_browser() unconditionally calls SessionManager().save()
# after a successful login, and by default that goes through the macOS
# Keychain. On this machine (unattended/background shell, no unlocked GUI
# keychain session) that write fails with keyring.backends.macOS.api.Error:
# (-61, 'Unknown Error') -- raised as an uncaught KeyringError that kills the
# whole process right after login succeeds, before create_draft() ever runs
# (verified 2026-07-12: login completed and cookies were extracted every
# time, but the script produced no DRAFT_KEY and no visible error because the
# traceback landed in a subshell's captured $OUT, not this script's own
# stdout/stderr). note_mcp ships a built-in escape hatch for exactly this
# ("For Docker/headless environments where keyring is not available, set
# USE_FILE_SESSION=1" -- see note_mcp/auth/session.py), which stores the
# session as a plain JSON file instead. Use it always here.
export USE_FILE_SESSION=1
# Default points at the real clone (~/.openclaw/external/note-mcp): the
# ~/.cache/anicca-clones/note-mcp path was never populated on this machine
# (verified 2026-07-12: dir does not exist), so every unattended run of this
# script hit "FATAL: note-mcp not cloned" before ever reaching login. The
# 2026-07-12 fix further below already assumed callers would override
# NOTE_MCP_DIR to the real location "as we do" -- but nothing in the daily
# loop actually sets that override, so make the working default correct
# instead of relying on an override that doesn't exist.
NOTE_MCP_DIR="${NOTE_MCP_DIR:-$HOME/.openclaw/external/note-mcp}"

[[ -d "$NOTE_MCP_DIR" ]] || { echo "FATAL: note-mcp not cloned at $NOTE_MCP_DIR" >&2; exit 2; }

# note-mcp venv (checked early — needed both for create_draft below and for the
# stage1-manifest title patch that runs before it).
PY_VENV="$NOTE_MCP_DIR/.venv/bin/python"
bash "$SCRIPT_DIR/ensure-note-mcp-runtime.sh" "$NOTE_MCP_DIR"

# venv-cloak python (has the `cloakbrowser` lib note-stage1-render.py needs for the
# table→PNG headless screenshot step; note-mcp's own venv does NOT have cloakbrowser).
CLOAK_PY="$HOME/.openclaw/skills/_shared/venv-cloak/bin/python3"

# Ensure camofox daemon is up
curl -sS --max-time 3 http://localhost:9377/health >/dev/null 2>&1 || \
  bash "$HOME/.openclaw/skills/camofox-browser/scripts/start.sh" >/dev/null 2>&1 || true
sleep 2

# Step 1: Login via camofox if needed, extract _note_session_v5 cookie
# Approach: fresh tab → /login → fill email + pw → submit → wait for redirect → read live cookies.sqlite
USER_ID="${ANICCA_NOTE_USER_ID:-anicca-pure}"
SESSION_KEY="${ANICCA_NOTE_SESSION_KEY:-note-daily}"

# Always probe live cookies first — if session is already alive, skip login.
# Camoufox/Playwright profiles in this environment live under /private/tmp,
# not /var/folders, so search both and pick the newest matching profile.
PROFILE="$(python3 "$SCRIPT_DIR/find-note-browser-profile.py" \
  --root /private/tmp:1 --root /var/folders:4)"
COOKIE_DB=""
if [[ -n "$PROFILE" && -f "$PROFILE/cookies.sqlite" ]]; then
  COOKIE_DB="/tmp/note-publish-cookies-$$.sqlite"
  cp "$PROFILE/cookies.sqlite" "$COOKIE_DB" 2>/dev/null || COOKIE_DB=""
fi
USE_DIRECT_LOGIN=false

NEED_LOGIN=true
if [[ -n "$COOKIE_DB" ]]; then
  SV5=$(sqlite3 "$COOKIE_DB" "SELECT value FROM moz_cookies WHERE host LIKE '%note.com%' AND name='_note_session_v5' LIMIT 1;" 2>/dev/null || true)
  if [[ -n "$SV5" ]]; then
    # quick auth probe: POST to /v1/text_notes with index:1 should NOT return "not_login"
    COOKIE_HDR=$(sqlite3 "$COOKIE_DB" "SELECT name || '=' || value FROM moz_cookies WHERE host LIKE '%note.com%';" | tr '\n' ';' | sed 's/;/; /g; s/; $//')
    PROBE=$(curl -sS --max-time 10 -X POST "https://note.com/api/v1/text_notes" \
      -H "Cookie: $COOKIE_HDR" \
      -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36" \
      -H "Content-Type: application/json" \
      -H "Origin: https://editor.note.com" \
      -H "Referer: https://editor.note.com/" \
      -d '{"index":1}' 2>/dev/null || echo '{}')
    if echo "$PROBE" | grep -q '"can_publish":true'; then
      NEED_LOGIN=false
      # delete the probe note (we created one) — best effort, ignore failure
      PROBE_ID=$(echo "$PROBE" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['data']['id'])" 2>/dev/null || true)
      if [[ -n "$PROBE_ID" ]]; then
        curl -sS --max-time 10 -X DELETE "https://note.com/api/v1/text_notes/$PROBE_ID" \
          -H "Cookie: $COOKIE_HDR" \
          -H "User-Agent: Mozilla/5.0" \
          -H "Origin: https://editor.note.com" \
          -H "Referer: https://editor.note.com/" >/dev/null 2>&1 || true
      fi
    fi
  fi
fi

if [[ "$NEED_LOGIN" == "true" ]]; then
  echo "note.com: logging in via camofox ($NOTE_EMAIL)" >&2
  TAB_ID=$(curl -sS --max-time 10 -X POST http://localhost:9377/tabs \
    -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg u "$USER_ID" --arg s "$SESSION_KEY" '{url:"https://note.com/login",userId:$u,sessionKey:$s}')" \
    | jq -r '.tabId // empty' 2>/dev/null || true)
  sleep 5
  if [[ -n "$TAB_ID" ]]; then
    # This tab automation is only a fast path. Every request is bounded and
    # best-effort so a stale camofox tab cannot prevent note_mcp's independent
    # direct-login fallback below.
    curl -sS --max-time 10 -X POST "http://localhost:9377/tabs/$TAB_ID/type" \
      -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg t "$NOTE_EMAIL" --arg u "$USER_ID" --arg s "$SESSION_KEY" '{ref:"e5",text:$t,userId:$u,sessionKey:$s}')" >/dev/null || true
    sleep 1
    curl -sS --max-time 10 -X POST "http://localhost:9377/tabs/$TAB_ID/type" \
      -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg t "$NOTE_PASSWORD" --arg u "$USER_ID" --arg s "$SESSION_KEY" '{ref:"e6",text:$t,userId:$u,sessionKey:$s}')" >/dev/null || true
    sleep 1
    curl -sS --max-time 10 -X POST "http://localhost:9377/tabs/$TAB_ID/click" \
      -H 'Content-Type: application/json' \
      -d "$(jq -nc --arg u "$USER_ID" --arg s "$SESSION_KEY" '{ref:"e9",userId:$u,sessionKey:$s}')" >/dev/null || true
    sleep 8
  fi
  # Re-extract cookies
  PROFILE="$(python3 "$SCRIPT_DIR/find-note-browser-profile.py" \
    --root /private/tmp:1 --root /var/folders:4)"
  # camofox tab-based login is a best-effort fast path only. If no profile shows up
  # (camofox naming drift, timing, or a note.com login-page DOM change breaking the
  # ref-based type/click), fall through with no cookie: the block below already sets
  # USE_DIRECT_LOGIN=true whenever COOKIE_DB is empty, and note_mcp's own
  # login_with_browser() then performs a real, independent login. A hard exit here used
  # to kill the whole script before that fallback ever ran (observed 2026-07-15: profile
  # search came up empty and the script died with exit 3 instead of falling through).
  if [[ -n "$PROFILE" && -f "$PROFILE/cookies.sqlite" ]]; then
    COOKIE_DB="/tmp/note-publish-cookies-$$.sqlite"
    cp "$PROFILE/cookies.sqlite" "$COOKIE_DB" 2>/dev/null || COOKIE_DB=""
  fi
fi

if [[ -z "${COOKIE_DB:-}" || ! -f "$COOKIE_DB" ]]; then
  USE_DIRECT_LOGIN=true
else
  SV5=$(sqlite3 "$COOKIE_DB" "SELECT value FROM moz_cookies WHERE host LIKE '%note.com%' AND name='_note_session_v5' LIMIT 1;" 2>/dev/null || true)
  [[ -n "$SV5" ]] || USE_DIRECT_LOGIN=true
fi

# Step 1.5: render tables → PNG, mermaid → source-captured, via note-stage1-render.py
# (note-mcp does not touch note.com here — this is a local headless-browser render only).
# WORK is a persistent per-run dir under ~/.cloak (NEVER /tmp — reboot/disk-cleanup wipes it
# mid-task), and deliberately NOT the shared ~/.cloak/note-work/note-stage dir the manual
# Automaton pipeline uses, so a daily-loop run can never clobber that pipeline's manifest.
WORK="$HOME/.cloak/note-work/note-stage-daily/$$-$(date +%s)"
mkdir -p "$WORK"
IMG_DIR_SLUG="$(basename "$MD_FILE" | sed -E 's/\.[Mm][Dd]$//; s/[^A-Za-z0-9_-]+/-/g')"
STAGE1_OK=false
# NOTE: `if VAR=$(cmd); then ...; else RC=$?; fi` (not `VAR=$(cmd); RC=$?`) — under `set -e`,
# a bare `VAR=$(cmd)` assignment that fails kills the whole script right there, before the
# next line even runs; putting the assignment in an `if` condition is what suspends -e for it.
if STAGE1_LOG=$(NOTE_WORK="$WORK" NOTE_IMG_DIR="$IMG_DIR_SLUG" "$CLOAK_PY" "$SCRIPT_DIR/note-stage1-render.py" "$MD_FILE" 2>&1); then
  STAGE1_RC=0
else
  STAGE1_RC=$?
fi
if [[ $STAGE1_RC -eq 0 && -f "$WORK/note-manifest.json" ]]; then
  STAGE1_OK=true
  echo "note.com: stage1 render OK — $STAGE1_LOG" >&2
  # keep the manifest's title in sync with --title (the manifest derives its own title from
  # the markdown's H1, which is usually the same string but must not silently diverge from
  # what the caller asked for; create_draft below and stage2's update_article both need to
  # agree on one title).
  TITLE="$TITLE" "$PY_VENV" - "$WORK/note-manifest.json" <<'PY' || echo "WARN: manifest title patch failed — continuing with the H1-derived title" >&2
import json, os, sys
p = sys.argv[1]
m = json.load(open(p))
m["title"] = os.environ["TITLE"]
json.dump(m, open(p, "w"), ensure_ascii=False)
PY
else
  echo "WARN: note-stage1-render failed (rc=$STAGE1_RC) — falling back to raw markdown body (tables/mermaid will not render as images on note.com):" >&2
  printf '%s\n' "$STAGE1_LOG" >&2
fi

# Step 2: publish via note-mcp Python lib
# DRAFT-ONLY, permanently (Dais 2026-07-12): this loop must never put an unreviewed
# article on the public internet. There used to be a publish_article() call right after
# create_draft() below that went live immediately -- it is deliberately gone, and only
# create_draft is imported now so a future edit can't casually call it back in.
# Publishing is a human decision Dais makes from the note.com dashboard after reading
# the draft; there is no code path here that can do it for him.
MANIFEST_ARG=""; [[ "$STAGE1_OK" == "true" ]] && MANIFEST_ARG="$WORK/note-manifest.json"

# Step 1.8: idempotency — decide update-existing-draft vs create-new-draft via the LOCAL ledger
# (~/.cloak/note-work/draft-ledger.json by default, see note-draft-ledger.py). This is a pure
# local decision (no network): --new-draft always wins (escape hatch), --key always wins over
# the ledger (explicit override), otherwise the ledger is consulted by MD_FILE's absolute path.
# Best-effort: a broken ledger must never turn a working publish into a script failure, so a
# failure here falls back to the pre-idempotency behavior (always create a new draft).
if RESOLVE_JSON=$("$PY_VENV" "$SCRIPT_DIR/note-draft-ledger.py" resolve --md "$MD_FILE" ${DRAFT_KEY_ARG:+--key "$DRAFT_KEY_ARG"} ${NEW_DRAFT_FLAG:+--new-draft} 2>&1); then
  LEDGER_ACTION=$(printf '%s' "$RESOLVE_JSON" | jq -r '.action // "create"' 2>/dev/null || echo create)
  LEDGER_ARTICLE_ID=$(printf '%s' "$RESOLVE_JSON" | jq -r '.article_id // empty' 2>/dev/null || echo "")
  LEDGER_KEY_FORMAT_OK=$(printf '%s' "$RESOLVE_JSON" | jq -r '.key_format_ok // false' 2>/dev/null || echo false)
  echo "note.com: ledger resolve -> $RESOLVE_JSON" >&2
else
  echo "WARN: note-draft-ledger.py resolve failed — defaulting to create-new-draft: $RESOLVE_JSON" >&2
  LEDGER_ACTION="create"; LEDGER_ARTICLE_ID=""; LEDGER_KEY_FORMAT_OK="false"
fi

# same `if VAR=$(cmd); then/else` requirement as above — a bare `OUT=$(...)` here would let
# `set -e` kill the script on any create_draft failure before the FATAL branch below ever runs.
if OUT=$(COOKIE_DB="$COOKIE_DB" USE_DIRECT_LOGIN="$USE_DIRECT_LOGIN" NOTE_EMAIL="$NOTE_EMAIL" NOTE_PASSWORD="$NOTE_PASSWORD" MD_FILE="$MD_FILE" TITLE="$TITLE" DESCRIPTION="$DESCRIPTION" TAGS_RAW="$TAGS_RAW" NOTE_MCP_DIR="$NOTE_MCP_DIR" NOTE_MANIFEST="$MANIFEST_ARG" LEDGER_ACTION="$LEDGER_ACTION" LEDGER_ARTICLE_ID="$LEDGER_ARTICLE_ID" LEDGER_KEY_FORMAT_OK="$LEDGER_KEY_FORMAT_OK" \
  "$PY_VENV" - <<'PY' 2>&1
import json, os, re, sys, asyncio, time, subprocess, tempfile
# NOTE_MCP_DIR-relative src path (2026-07-12 fix): this used to hardcode
# ~/.cache/anicca-clones/note-mcp/src regardless of $NOTE_MCP_DIR, which silently worked
# only because uv sync installs note_mcp into the venv's own site-packages (editable
# install) -- so the bogus insert was a no-op, not a real fix. Use the actual configured
# dir so a caller pointing NOTE_MCP_DIR elsewhere (as we do, at ~/.openclaw/external/note-mcp)
# gets the right src tree if the editable install ever isn't in play.
sys.path.insert(0, os.path.join(os.environ.get("NOTE_MCP_DIR", os.path.expanduser("~/.cache/anicca-clones/note-mcp")), "src"))
from note_mcp.models import Session, ArticleInput, ArticleStatus
from note_mcp.api.articles import create_draft, get_article_via_api, update_article
from note_mcp.auth.browser import login_with_browser
from note_mcp.auth.session import SessionManager

async def load_session():
  saved = SessionManager().load()
  if saved and not saved.is_expired() and saved.cookies:
    return saved
  if os.environ.get("USE_DIRECT_LOGIN", "false").lower() == "true":
    return await login_with_browser(
      credentials=(os.environ["NOTE_EMAIL"], os.environ["NOTE_PASSWORD"])
    )

  res = subprocess.check_output(['sqlite3', os.environ['COOKIE_DB'],
    "SELECT name || '=' || value FROM moz_cookies WHERE host LIKE '%note.com%';"]).decode()
  cookies = {}
  for line in res.strip().split('\n'):
    if '=' in line:
      k,v = line.split('=', 1); cookies[k] = v
  return Session(cookies=cookies, user_id=os.environ.get("NOTE_USER_ID", "14651590"), username=os.environ.get("NOTE_URLNAME", "anicca123"), created_at=int(time.time()))

session = asyncio.run(load_session())
# cache this session's cookies for note-stage2-publish.py (reads them from this fixed path,
# same convention as scripts/note-publish/extract-note-cookies.py) so stage2 does not need
# its own independent login.
try:
  cookie_out = os.path.expanduser("~/.cloak/note-work/note-cookies.json")
  cookie_dir = os.path.dirname(cookie_out)
  os.makedirs(cookie_dir, exist_ok=True)
  # atomic write (same-dir temp + os.replace): note-stage2-publish.py and the manual
  # note-publish/*.py scripts read this file concurrently, and a bare open(...,"w") would
  # let them observe a truncated/partial JSON mid-write.
  fd, tmp_path = tempfile.mkstemp(dir=cookie_dir, prefix=".note-cookies-", suffix=".tmp")
  try:
    with os.fdopen(fd, "w") as f:
      json.dump(session.cookies, f)
    os.replace(tmp_path, cookie_out)
  except Exception:
    os.unlink(tmp_path)
    raise
except Exception as e:
  print(f"WARN: could not cache session cookies for stage2: {e}", file=sys.stderr)

manifest_path = os.environ.get('NOTE_MANIFEST') or ''
if manifest_path and os.path.exists(manifest_path):
  body = json.load(open(manifest_path))['body']
else:
  raw = open(os.environ['MD_FILE']).read()
  body = re.sub(r'^---\n.*?\n---\n', '', raw, count=1, flags=re.S)
tags = [t.strip() for t in (os.environ.get('TAGS_RAW') or '').split(',') if t.strip()]
article = ArticleInput(
  title=os.environ['TITLE'],
  body=body,
  description=os.environ.get('DESCRIPTION') or None,
  tags=tags or []
)
async def main():
  # DRAFT-ONLY, permanently: stop right here. No publish_article() call. The draft key
  # below is what a human uses to find and review it in the note.com dashboard.
  #
  # Idempotency (2026-07-16): if the ledger (or an explicit --key) resolved to an existing
  # draft, update it via update_article() instead of creating a brand-new one. SAFETY: never
  # call update_article on an article that is no longer a draft (e.g. it was published by a
  # human from the dashboard in the meantime) -- that would silently rewrite a LIVE public
  # article's content, which this loop must never do. So before trusting a ledger/--key
  # article_id we do a live status check via get_article_via_api(), and fall back to creating
  # a brand-new draft (the pre-idempotency, always-safe behavior) whenever that check fails or
  # shows anything other than status=DRAFT. get_article_via_api() itself REJECTS numeric IDs
  # (see note_mcp/api/articles.py, verified by reading the source) and only accepts key-format
  # ids ("n" + non-digit) -- LEDGER_KEY_FORMAT_OK (computed locally by note-draft-ledger.py) is
  # that same check done ahead of time so a numeric-only ledger entry never even reaches this
  # network call.
  action = os.environ.get('LEDGER_ACTION', 'create')
  article_id = os.environ.get('LEDGER_ARTICLE_ID') or None
  key_format_ok = os.environ.get('LEDGER_KEY_FORMAT_OK', 'false').lower() == 'true'
  reused = False
  key = None
  num = None
  if action == 'update' and article_id and key_format_ok:
    safe_to_update = False
    try:
      existing = await get_article_via_api(session, article_id)
      safe_to_update = existing.status == ArticleStatus.DRAFT
      if not safe_to_update:
        print(f"WARN: ledger article_id {article_id} is not a draft anymore (status={existing.status!r}) -- creating a NEW draft instead of updating it", file=sys.stderr)
    except Exception as e:
      print(f"WARN: could not verify ledger article_id {article_id} before update ({e}) -- creating a NEW draft instead", file=sys.stderr)
    if safe_to_update:
      updated = await update_article(session, article_id, article)
      key = article_id
      num = getattr(updated, 'id', None) or None
      reused = True
  elif action == 'update' and article_id and not key_format_ok:
    print(f"WARN: ledger article_id {article_id!r} is not key-format -- creating a NEW draft instead of risking update_article on a numeric id", file=sys.stderr)

  if not reused:
    draft = await create_draft(session, article)
    key = getattr(draft, 'key', None)
    num = getattr(draft, 'id', None)
    if not key:
      raise RuntimeError("create_draft succeeded but draft.key was missing")

  print(f"DRAFT_KEY={key}")
  print(f"DRAFT_REUSED={'true' if reused else 'false'}")
  if num:
    print(f"DRAFT_NUM={num}")
asyncio.run(main())
PY
); then
  EC=0
else
  EC=$?
fi
rm -f "$COOKIE_DB" 2>/dev/null
if [[ $EC -ne 0 ]]; then
  echo "FATAL: draft creation failed:" >&2
  printf '%s\n' "$OUT" >&2
  exit $EC
fi
# same `if VAR=$(cmd); then/else` requirement as STAGE1_LOG/OUT above (see the comment at the
# top of the stage1 block): a bare `VAR=$(grep ... | head | cut)` with no grep match is a
# pipefail-triggering non-zero pipeline, which under `set -e` kills the script right here,
# before the FATAL branch below ever runs — silently turning "draft key missing" into an
# unlabeled generic failure instead of the intended FATAL message + exit 7.
if DRAFT_KEY=$(printf '%s\n' "$OUT" | grep -oE 'DRAFT_KEY=.+' | head -1 | cut -d= -f2-); then :; else DRAFT_KEY=""; fi
if [[ -z "$DRAFT_KEY" ]]; then
  echo "FATAL: draft creation succeeded but draft key not found in output" >&2
  printf '%s\n' "$OUT" >&2
  exit 7
fi
if DRAFT_NUM=$(printf '%s\n' "$OUT" | grep -oE 'DRAFT_NUM=.+' | head -1 | cut -d= -f2-); then :; else DRAFT_NUM=""; fi
if DRAFT_REUSED=$(printf '%s\n' "$OUT" | grep -oE 'DRAFT_REUSED=.+' | head -1 | cut -d= -f2-); then :; else DRAFT_REUSED="false"; fi

# Idempotency: persist this MD file -> draft key mapping for the NEXT run, whether this run
# reused an existing draft or created a new one (either way, the ledger must now point at
# DRAFT_KEY so a future re-run of this same file updates it instead of creating yet another
# draft). Best-effort — same reasoning as the resolve step above: a ledger write failure must
# never turn an already-successful draft into a script failure.
if ! "$PY_VENV" "$SCRIPT_DIR/note-draft-ledger.py" record --md "$MD_FILE" --key "$DRAFT_KEY" --num "$DRAFT_NUM" --title "$TITLE" >/dev/null 2>&1; then
  echo "WARN: could not record draft ledger entry for $MD_FILE (non-fatal — next run will create a new draft instead of reusing key=$DRAFT_KEY)" >&2
fi

# Step 3: embed tables/mermaid as images into the just-created draft via note-stage2-publish.py
# (update_article → draft_save?is_temp_saved=true, same DRAFT-ONLY guarantee as create_draft
# above — see the header comment at the top of note-stage2-publish.py). Best-effort: the draft
# from Step 2 already exists and is reviewable even if this step fails, so a failure here is a
# warning, not a fatal error — it must never turn an existing draft into a script failure.
# STAGE2_OK feeds the machine-readable stage1_ok/stage2_ok pair on the final stdout line below
# (the only stdout run.sh captures for this script) — false means "images were NOT embedded",
# whether because stage2 itself failed OR was skipped (stage1 unavailable / no DRAFT_NUM); either
# way the draft still shows raw @@TBL@@/@@FIG@@ markers instead of images, so collapsing those
# cases into one boolean is the correct, not lossy, signal for a caller deciding "does this draft
# need a manual look before publish".
STAGE2_OK=false
STAGE2_EMBEDDED=""
if [[ "$STAGE1_OK" == "true" && -n "$DRAFT_NUM" ]]; then
  echo "note.com: embedding tables/mermaid as images (draft NUM=$DRAFT_NUM KEY=$DRAFT_KEY)" >&2
  # NOTE_KEY (added alongside the pre-existing NOTE_NUM wiring): note-stage2-publish.py needs the
  # article KEY, not NUM, to call update_article whenever the body contains an embed-worthy
  # standalone URL (youtube/twitter/note.com/github/...) — passing NUM there hits a real note-mcp
  # bug (verified in note_mcp/api/articles.py update_article: it tries to resolve the key via
  # get_article_via_api(numeric_id), which itself rejects numeric IDs). DRAFT_KEY is already
  # available here from create_draft()'s output, same as DRAFT_NUM.
  if STAGE2_OUT=$(NOTE_WORK="$WORK" NOTE_NUM="$DRAFT_NUM" NOTE_KEY="$DRAFT_KEY" NOTE_SRC="$MD_FILE" NOTE_TAGS="$TAGS_RAW" NOTE_MCP_SRC="$NOTE_MCP_DIR/src" \
    "$PY_VENV" "$SCRIPT_DIR/note-stage2-publish.py" 2>&1); then
    STAGE2_RC=0
  else
    STAGE2_RC=$?
  fi
  # note-stage2-publish.py prints "EMBED_SUMMARY embedded=N/M failed=..." on its own stdout right
  # before exiting non-zero on ANY partial embed failure (draft is still saved either way — see
  # its own header). Same `if VAR=$(cmd); then :; else VAR=""; fi` guard as DRAFT_KEY/DRAFT_NUM
  # above: under `set -o pipefail` a grep-no-match must not kill this script via `set -e`.
  if STAGE2_EMBEDDED=$(printf '%s\n' "$STAGE2_OUT" | grep -oE 'embedded=[0-9]+/[0-9]+' | head -1 | cut -d= -f2); then :; else STAGE2_EMBEDDED=""; fi
  if [[ $STAGE2_RC -ne 0 ]]; then
    echo "WARN: stage2 image embedding failed (rc=$STAGE2_RC, embedded=${STAGE2_EMBEDDED:-unknown}) — draft key=${DRAFT_KEY} still exists but some images may show a [画像の埋め込みに失敗しました] placeholder instead of the real image. Review by hand." >&2
  else
    STAGE2_OK=true
    echo "note.com: stage2 embedding OK (embedded=${STAGE2_EMBEDDED:-unknown})" >&2
  fi
  printf '%s\n' "$STAGE2_OUT" >&2
else
  echo "note.com: skipping image embedding (stage1 render unavailable) — draft key=${DRAFT_KEY} has raw markdown tables/mermaid syntax" >&2
fi

if [[ -n "${ARTICLE_RUN_DIR:-}" || -n "${ARTICLE_PUBLICATION_STATE:-}" || -n "${ARTICLE_LEDGER:-}" ]]; then
  if [[ "$STAGE2_OK" != "true" || ! "$STAGE2_EMBEDDED" =~ ^[1-9][0-9]*/[1-9][0-9]*$ ]]; then
    if [[ "${ARTICLE_PUBLICATION_POLICY:-strict}" == "continuous" ]]; then
      echo "WARN: continuous publication records the body-image failure and continues with the saved note draft" >&2
    else
      echo "FATAL: managed note staging requires at least one fully embedded body image" >&2
      exit 8
    fi
  fi
  python3 "$SCRIPT_DIR/publication-guard.py" register-intent \
    --pair note/ja --target-kind note-key --target "$DRAFT_KEY" >/dev/null || exit $?
fi

# Design note (adversary FAIL fix, see run.sh for the reader side): this one line is the ONLY
# stdout run.sh's `URL="$(bash publish-note.sh ...)"` capture sees for the note channel — every
# other line above went to stderr. run.sh already stores that whole captured string verbatim as
# a free-form "release_url" (it was never a real URL for note.com even before this change, since
# this loop is draft-only), so embedding stage1_ok=/stage2_ok=/reused= as extra key=value tokens
# on this SAME line keeps that existing single-line contract intact (no new stdout line, no
# schema change to what run.sh treats as opaque text) while giving run.sh something grep-able to
# lift stage1_ok/stage2_ok/reused out and record into meta.json + account-history — reused=true
# means this run updated an EXISTING draft (idempotency ledger hit) instead of creating a new one.
echo "DRAFT (unpublished) key=${DRAFT_KEY} reused=${DRAFT_REUSED} stage1_ok=${STAGE1_OK} stage2_ok=${STAGE2_OK} stage2_embedded=${STAGE2_EMBEDDED} — review and publish by hand from the note.com dashboard"
