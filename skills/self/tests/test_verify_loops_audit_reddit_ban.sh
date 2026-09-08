#!/usr/bin/env bash
# test_verify_loops_audit_reddit_ban.sh — G2 item1 regression test (2026-07-11 loop-arch redesign /
# LOOPS-TRUTH-AUDIT.md: "liveurl HTTP200のみ→reddit BAN...を全て見逃す"). Before this fix, a BANNED
# reddit account (real incident: u/anicca_sao, confirmed via screenshot 2026-07-11) with a
# fresh-mtime, HTTP-200 newest post URL never re-triggered self-fix, because only the post URL's
# own HTTP code was checked -- never the ACCOUNT's own liveness. Proves: (a) a banned account
# escalates even when its own post URL is fresh+LIVE, (b) a healthy account (post 200, profile 200)
# does NOT escalate via the ban path, (c) a generic-title 404 (username never registered, not the
# same as banned) does NOT falsely trigger BANNED. Mirrors this codebase's own
# test_verify_loops_audit_reddit_liveness.sh stubbing convention (fake SELF dir + fake curl on
# PATH via VERIFY_LOOPS_AUDIT_CURL_BIN), extended with a URL-aware fake curl so the post-URL check
# and the account-profile check can return different canned responses in the same run.
set -uo pipefail
P=0; F=0
ok(){ echo "  ok $1"; P=$((P+1)); }
fail(){ echo "  FAIL $1"; F=$((F+1)); }

REAL_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/verify-loops-audit.sh"

setup(){
  FAKE_SELF="$(mktemp -d)"
  FAKE_HOME="$(mktemp -d)"
  FAKE_BIN="$(mktemp -d)"
  REDDIT_STATE="$FAKE_HOME/.local/state/life-manager/reddit/state"
  mkdir -p "$FAKE_HOME/.openclaw/state" "$FAKE_HOME/.openclaw/logs" \
           "$REDDIT_STATE" "$FAKE_SELF/life-manager-loop/state"

  cat > "$FAKE_SELF/verify-loops.sh" <<'EOF'
#!/usr/bin/env bash
echo "stub verify-loops output"
EOF
  cat > "$FAKE_SELF/cadence-deadline-check.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  cat > "$FAKE_SELF/cadence-evidence.py" <<'PYEOF'
import json, sys
loop = sys.argv[2]
print(json.dumps({"loop": loop, "met": True, "streak": 1, "scorecard": "ok"}))
PYEOF
  SELF_FIX_CALLS="$FAKE_HOME/.openclaw/state/self-fix-calls.log"
  cat > "$FAKE_SELF/self-fix.sh" <<EOF
#!/usr/bin/env bash
echo "\$1" >> "$SELF_FIX_CALLS"
EOF
  chmod +x "$FAKE_SELF/self-fix.sh" "$FAKE_SELF/verify-loops.sh" "$FAKE_SELF/cadence-deadline-check.sh"
}
mkroot(){ mkdir -p "$FAKE_HOME/.cloak"; cat > "$FAKE_HOME/.cloak/reddit-accounts.json" <<'JSONEOF'
[{"handle": "anicca_sao"}]
JSONEOF
}
reddit_call_count(){ local f="$FAKE_HOME/.openclaw/state/self-fix-calls.log"; [ -f "$f" ] || { echo 0; return; }; local n; n="$(grep -c '^reddit$' "$f" 2>/dev/null)"; echo "${n:-0}"; }

# URL-aware fake curl: distinguishes the account-profile URL (contains /user/) from the post URL,
# so a single run can return LIVE for the post while returning BANNED/OK for the profile.
fake_curl(){ # $1=post http code  $2=profile http code  $3=profile <title> text
  cat > "$FAKE_BIN/fake-curl" <<EOF
#!/usr/bin/env bash
out="/dev/null"; prev=""; url=""
for a in "\$@"; do
  if [ "\$prev" = "-o" ]; then out="\$a"; fi
  prev="\$a"; url="\$a"
done
case "\$url" in
  */user/*) code="$2"; title="$3" ;;
  *) code="$1"; title="" ;;
esac
if [ "\$out" != "/dev/null" ]; then printf '<title>%s</title>' "\$title" > "\$out"; fi
printf '%s' "\$code"
EOF
  chmod +x "$FAKE_BIN/fake-curl"
}

run(){ HOME="$FAKE_HOME" REDDIT_STATE_DIR="$REDDIT_STATE" VERIFY_LOOPS_SELF_DIR="$FAKE_SELF" VERIFY_LOOPS_AUDIT_CURL_BIN="$FAKE_BIN/fake-curl" bash "$REAL_SCRIPT" >/dev/null 2>&1; }

# --- scenario 1 (THE FIX): fresh post URL, LIVE(200) -- but the ACCOUNT is BANNED using the
# production contract's distinctive profile 403 + suspended title -> escalation MUST fire ---
setup; mkroot
echo '{"url": "https://old.reddit.com/r/test/comments/live/x/", "account": "anicca_sao"}' > "$REDDIT_STATE/posts.jsonl"
fake_curl 200 403 "reddit.com: suspended"
run
[ "$(reddit_call_count)" = 1 ] && ok "fresh+LIVE post but BANNED account -> 1 reddit self-fix call (the fix)" \
  || fail "fresh+LIVE post but BANNED account: expected 1 call, got $(reddit_call_count) (BAN check not wired)"
grep -qi "BANNED" "$FAKE_HOME/.openclaw/state/self-fix-calls.log" 2>/dev/null || true
rm -rf "$FAKE_SELF" "$FAKE_HOME" "$FAKE_BIN"

# --- scenario 2 (no false positive): fresh+LIVE post, account profile also 200 (healthy,
# not banned) -> NO escalation ---
setup; mkroot
echo '{"url": "https://old.reddit.com/r/test/comments/live2/x/", "account": "anicca_sao"}' > "$REDDIT_STATE/posts.jsonl"
fake_curl 200 200 "overview for anicca_sao"
run
[ "$(reddit_call_count)" = 0 ] && ok "fresh+LIVE post + healthy(200) account -> 0 reddit self-fix calls (no false BAN)" \
  || fail "fresh+LIVE + healthy account: expected 0 calls, got $(reddit_call_count)"
rm -rf "$FAKE_SELF" "$FAKE_HOME" "$FAKE_BIN"

# --- scenario 3 (no false positive on a GENERIC 404): fresh+LIVE post, profile 404 but with the
# GENERIC reddit.com title (username never embedded -- e.g. a typo'd/never-registered name, NOT
# the same signal as a banned/suspended/deleted account) -> must NOT be treated as BANNED ---
setup; mkroot
echo '{"url": "https://old.reddit.com/r/test/comments/live3/x/", "account": "anicca_sao"}' > "$REDDIT_STATE/posts.jsonl"
fake_curl 200 404 "reddit.com: page not found"
run
[ "$(reddit_call_count)" = 0 ] && ok "fresh+LIVE post + generic-404 profile (no username in title) -> 0 calls (not falsely BANNED)" \
  || fail "generic 404 title: expected 0 calls (not banned), got $(reddit_call_count)"
rm -rf "$FAKE_SELF" "$FAKE_HOME" "$FAKE_BIN"

echo "=== test_verify_loops_audit_reddit_ban: $P passed $F failed ==="
[ "$F" = 0 ] && exit 0 || exit 1
