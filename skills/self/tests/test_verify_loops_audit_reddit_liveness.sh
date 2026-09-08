#!/usr/bin/env bash
# test_verify_loops_audit_reddit_liveness.sh — self-heal.md root cause #3 regression test.
# Before the fix, verify-loops-audit.sh's reddit escalation only checked stale_hrs(posts.jsonl)>=30
# — a FRESH-mtime-but-DEAD newest-post URL (the real reddit 403 incident self-heal.md documents)
# never re-triggered self-fix, because a 403'd account can still keep writing fresh-looking rows.
# This proves the fix: escalation is now `stale_hrs>=30 OR liveurl==DEAD`. Builds a fake SELF dir
# (stub verify-loops.sh/cadence-evidence.py/cadence-deadline-check.sh/self-fix.sh, mirroring this
# codebase's own test_cadence_deadline_check.sh stubbing convention) plus a fake `curl` on PATH
# (env-driven fake HTTP code) so this test never hits the real network or spawns a real self-fix.
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
# 1 reddit account exists (else the escalation guard legitimately no-ops regardless of liveness).
mkroot(){ mkdir -p "$FAKE_HOME/.cloak"; cat > "$FAKE_HOME/.cloak/reddit-accounts.json" <<'JSONEOF'
[{"handle": "anicca_sao"}]
JSONEOF
}

# FIND-010 (this codebase's own documented grep -c pitfall, see verify-loops.sh::count()): grep -c
# prints "0" AND exits 1 on no match — chaining `||echo 0` after it double-prints. Read into a var first.
reddit_call_count(){ local f="$FAKE_HOME/.openclaw/state/self-fix-calls.log"; [ -f "$f" ] || { echo 0; return; }; local n; n="$(grep -c '^reddit$' "$f" 2>/dev/null)"; echo "${n:-0}"; }

fake_curl(){ # $1=http code to return for ALL curl -w '%{http_code}' calls
  cat > "$FAKE_BIN/fake-curl" <<EOF
#!/usr/bin/env bash
# minimal fake: honors -o /dev/null -w '%{http_code}' (verify-loops-audit.sh's liveurl() usage)
echo -n "$1"
EOF
  chmod +x "$FAKE_BIN/fake-curl"
}

run(){
  # VERIFY_LOOPS_AUDIT_CURL_BIN: the script's own PATH= line always puts the REAL system curl
  # ahead of anything a test could put on PATH, so this test-only seam (added alongside the fix)
  # is the only reliable way to stub the HTTP outcome without hitting the real network.
  HOME="$FAKE_HOME" REDDIT_STATE_DIR="$REDDIT_STATE" VERIFY_LOOPS_SELF_DIR="$FAKE_SELF" VERIFY_LOOPS_AUDIT_CURL_BIN="$FAKE_BIN/fake-curl" bash "$REAL_SCRIPT" >/dev/null 2>&1
}

# --- scenario 1: FRESH posts.jsonl (mtime=now, stale_hrs<30) + curl LIVE(200) -> NO escalation ---
setup; mkroot
echo '{"url": "https://old.reddit.com/r/test/comments/live/x/"}' > "$REDDIT_STATE/posts.jsonl"
fake_curl 200
run
[ "$(reddit_call_count)" = 0 ] && ok "fresh + LIVE(200) -> 0 reddit self-fix calls (no false escalation)" \
  || fail "fresh + LIVE(200): expected 0 calls, got $(reddit_call_count)"
rm -rf "$FAKE_SELF" "$FAKE_HOME" "$FAKE_BIN"

# --- scenario 2 (THE FIX): FRESH posts.jsonl (stale_hrs<30, would NOT escalate under the old
# stale_hrs-only condition) + curl DEAD(403, the real reddit incident) -> escalation MUST fire ---
setup; mkroot
echo '{"url": "https://old.reddit.com/r/test/comments/dead/x/"}' > "$REDDIT_STATE/posts.jsonl"
fake_curl 403
run
[ "$(reddit_call_count)" = 1 ] && ok "fresh + DEAD(403) -> 1 reddit self-fix call (the fix: OR liveurl==DEAD)" \
  || fail "fresh + DEAD(403): expected 1 call, got $(reddit_call_count) (liveness gate not wired)"
rm -rf "$FAKE_SELF" "$FAKE_HOME" "$FAKE_BIN"

# --- scenario 3 (pre-existing behavior preserved): STALE posts.jsonl (mtime old, stale_hrs>=30)
# + curl LIVE(200) -> escalation still fires via the stale_hrs arm ---
setup; mkroot
echo '{"url": "https://old.reddit.com/r/test/comments/stale/x/"}' > "$REDDIT_STATE/posts.jsonl"
OLD=$(( $(date +%s) - 3600*40 ))
python3 -c "import os,sys; t=float(sys.argv[1]); os.utime(sys.argv[2], (t,t))" "$OLD" "$REDDIT_STATE/posts.jsonl"
fake_curl 200
run
[ "$(reddit_call_count)" = 1 ] && ok "stale(40h) + LIVE(200) -> 1 reddit self-fix call (pre-existing stale_hrs arm intact)" \
  || fail "stale + LIVE(200): expected 1 call, got $(reddit_call_count) (regression on the OLD arm)"
rm -rf "$FAKE_SELF" "$FAKE_HOME" "$FAKE_BIN"

# --- scenario 4 (guard preserved): NO reddit account at all + DEAD -> still no escalation
# (legitimately pre-provision, per the existing NACC guard) ---
setup
rm -f "$FAKE_HOME/.cloak/reddit-accounts.json"
echo '{"url": "https://old.reddit.com/r/test/comments/noacct/x/"}' > "$REDDIT_STATE/posts.jsonl"
fake_curl 403
run
[ "$(reddit_call_count)" = 0 ] && ok "no account + DEAD(403) -> 0 reddit self-fix calls (NACC guard intact)" \
  || fail "no account + DEAD(403): expected 0 calls, got $(reddit_call_count)"
rm -rf "$FAKE_SELF" "$FAKE_HOME" "$FAKE_BIN"

echo "=== test_verify_loops_audit_reddit_liveness: $P passed $F failed ==="
[ "$F" = 0 ] && exit 0 || exit 1
