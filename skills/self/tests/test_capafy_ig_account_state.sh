#!/usr/bin/env bash
set -uo pipefail

P=0
F=0
ok(){ echo "  ok $1"; P=$((P+1)); }
fail(){ echo "  FAIL $1"; F=$((F+1)); }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="$ROOT/earn/capafy-marketing/account_state.sh"
ENGINE_STATE="$ROOT/earn/marketing-engine/account_state.sh"
ENGINE_PROMPT="$ROOT/earn/marketing-engine/provision_prompt.sh"
DAILY="$ROOT/earn/capafy-marketing/capafy-ig-marketing-daily.sh"
WARM="$ROOT/earn/capafy-marketing/warm_jitter.sh"
GOAL="$ROOT/earn/capafy-marketing/capafy-goal-monitor.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for required_file in "$HELPER" "$ENGINE_STATE" "$ENGINE_PROMPT"; do
  if [ ! -f "$required_file" ]; then
    fail "required helper exists: $required_file"
    echo "=== test_capafy_ig_account_state: $P passed $F failed ==="
    exit 1
  fi
done
# shellcheck source=/dev/null
. "$HELPER"
# shellcheck source=/dev/null
. "$ENGINE_PROMPT"

printf '[]\n' > "$TMP/accounts.json"
[ -z "$(resolve_capafy_ig_handle "$TMP/accounts.json")" ] \
  && ok "empty state returns no active account" \
  || fail "empty state must return no active account"

cat > "$TMP/accounts.json" <<'JSON'
[
  {"handle":"old_poisoned","status":"poisoned_manual_backup","session_owner":"browser"},
  {"handle":"older_ready","status":"ready","session_owner":"instagrapi"},
  {"handle":"new_warming","status":"warming_day1","session_owner":"browser","started_warming":"2026-07-19"}
]
JSON
[ "$(resolve_capafy_ig_handle "$TMP/accounts.json")" = "new_warming" ] \
  && ok "latest usable ready/warming account wins" \
  || fail "resolver did not return latest usable account"
[ "$(resolve_ig_handle "$TMP/accounts.json")" = "new_warming" ] \
  && ok "generic resolver matches Capafy shim" \
  || fail "generic resolver and Capafy shim differ"
[ "$(resolve_ig_session_owner "$TMP/accounts.json")" = "browser" ] \
  && ok "generic resolver returns active session owner" \
  || fail "generic resolver missed active session owner"
[ "$(resolve_ig_started_warming "$TMP/accounts.json")" = "2026-07-19" ] \
  && ok "generic resolver returns active started_warming" \
  || fail "generic resolver missed active started_warming"
[ "$(ig_warming_day "2026-07-19" "2026-07-19")" = "1" ] \
  && [ "$(ig_warming_day "2026-07-18" "2026-07-19")" = "2" ] \
  && [ "$(ig_warming_day "2026-07-17" "2026-07-19")" = "3" ] \
  && ok "warming day is elapsed days plus one" \
  || fail "warming day does not use day1=creation semantics"

cat > "$TMP/accounts.json" <<'JSON'
[
  {"handle":"flagged_ready","status":"ready","poisoned_at":"2026-07-19T00:00Z"},
  {"handle":"blocked","status":"blocked"}
]
JSON
[ -z "$(resolve_capafy_ig_handle "$TMP/accounts.json")" ] \
  && ok "poisoned or blocked accounts are rejected" \
  || fail "resolver returned a poisoned or blocked account"

MARKER="$TMP/cooked"
[ "$(capafy_ig_provision_reason "" "$MARKER")" = "no-active-account" ] \
  && ok "empty handle enters PROVISION" \
  || fail "empty handle did not enter PROVISION"
[ -z "$(capafy_ig_provision_reason "fresh_warming" "$MARKER")" ] \
  && ok "usable handle skips PROVISION" \
  || fail "usable handle unexpectedly enters PROVISION"
touch "$MARKER"
[ "$(capafy_ig_provision_reason "fresh_warming" "$MARKER")" = "cooked-marker" ] \
  && ok "cooked marker forces PROVISION" \
  || fail "cooked marker did not force PROVISION"

mkdir -p "$TMP/home-empty" "$TMP/home-active"
printf '[]\n' > "$TMP/empty.json"
EMPTY_PROBE="$(HOME="$TMP/home-empty" CAPAFY_IG_ACCOUNTS_FILE="$TMP/empty.json" CAPAFY_IG_PROBE_ONLY=1 bash "$DAILY")"
[ "$EMPTY_PROBE" = "active_handle=none provision_needed=yes reason=no-active-account" ] \
  && ok "daily dry probe enters PROVISION with empty state" \
  || fail "empty-state dry probe output: $EMPTY_PROBE"
cat > "$TMP/active.json" <<'JSON'
[{"handle":"fresh_warming","profile":"capafy-mkt-fresh","port":9247,"status":"warming","session_owner":"instagrapi"}]
JSON
ACTIVE_PROBE="$(HOME="$TMP/home-active" CAPAFY_IG_ACCOUNTS_FILE="$TMP/active.json" CAPAFY_IG_PROBE_ONLY=1 bash "$DAILY")"
[ "$ACTIVE_PROBE" = "active_handle=fresh_warming provision_needed=no reason=none" ] \
  && ok "daily dry probe resolves active handle" \
  || fail "active-state dry probe output: $ACTIVE_PROBE"
GOAL_PROBE="$(HOME="$TMP/home-active" CAPAFY_IG_ACCOUNTS_FILE="$TMP/active.json" \
  CAPAFY_ACCOUNT_STATE_HELPER="$HELPER" CAPAFY_GOAL_MONITOR_PROBE_ONLY=1 bash "$GOAL")"
[ "$GOAL_PROBE" = "active_handle=fresh_warming active_port=9247 accounts_path=$TMP/active.json" ] \
  && ok "goal monitor resolves handle and port from account state" \
  || fail "goal-monitor state probe output: $GOAL_PROBE"

if ! grep -q 'useclaudeskills' "$DAILY" "$WARM"; then
  ok "daily and warmup scripts contain no baked handle"
else
  fail "daily or warmup script still bakes useclaudeskills"
fi
if grep -Fq 'resolve_capafy_ig_handle "$ACCOUNTS_FILE"' "$GOAL" \
  && grep -Fq -- '--accounts-path "$ACCOUNTS_FILE"' "$GOAL" \
  && ! grep -Fq "sed -nE 's/^IG_HANDLE=" "$GOAL"; then
  ok "goal monitor uses account state instead of parsing daily source"
else
  fail "goal monitor is not fully wired to account state"
fi

grep -Fq 'PROVISION_NEEDED' "$DAILY" \
  && ok "daily keeps PROVISION gate" \
  || fail "daily lost PROVISION gate"
for caller in "$DAILY"; do
  grep -Fq 'render_ig_provision_prompt' "$caller" \
    && ok "caller uses shared provision renderer: $(basename "$caller")" \
    || fail "caller bypasses shared provision renderer: $(basename "$caller")"
done
for needle in 'DAY 1 SIGNUP ONLY' 'Do not run Client().login' 'Do not run login_by_sessionid' 'Do not create ~/.cloak/instagrapi-<handle>.json' '"status":"warming"' '"session_owner":"browser"' '"started_warming":"<today YYYY-MM-DD>"' 'provision-blocked:'; do
  grep -Fq "$needle" "$ENGINE_PROMPT" \
    && ok "shared prompt wires $needle" \
    || fail "shared prompt missing $needle"
done
if ! grep -Fq 'DURABLE GOLDEN SESSION' "$ENGINE_PROMPT" \
  && ! grep -Fq 'cl.login(<username>' "$ENGINE_PROMPT"; then
  ok "shared provision contains no day1 golden-session action"
else
  fail "shared provision still commands day1 golden-session creation"
fi
if grep -Fq 'IG_SESSION_OWNER' "$GOAL" \
  && grep -Fq 'ACCOUNT_DAY' "$GOAL" \
  && grep -Fq 'VERIFY_ELIGIBLE' "$GOAL"; then
  ok "goal monitor gates verify-only by session owner and account day"
else
  fail "goal monitor lacks owner/day verify-only gate"
fi

RENDERED_PROMPT="$(
  IG_PROVISION_ACCOUNT_STATE_FILE="$TMP/shared-state.json" \
  IG_PROVISION_HANDLE_PREFIX="testhandle" \
  IG_PROVISION_INSTANCE="test-instance" \
  IG_PROVISION_GMAIL_PLUS_TAG_PREFIX="testtag" \
  IG_PROVISION_BIO_TEXT="test bio, NO link" \
  IG_PROVISION_BROWSER_INSTRUCTIONS="test isolated browser context" \
  IG_PROVISION_PORT="9339" \
  IG_PROVISION_CONTEXT_ID="test-dedicated" \
  LIFE_MANAGER_REPO="$(git -C "$ROOT" rev-parse --show-toplevel)" \
  LIFE_MANAGER_GMAIL_ACCOUNT="owner@gmail.com" \
  render_ig_provision_prompt
)"
for needle in "$TMP/shared-state.json" 'testhandle' 'test-instance' 'owner+testtag<random-tag>@gmail.com' 'test bio, NO link' 'test isolated browser context' '"status":"warming"' '"session_owner":"browser"' '"started_warming":"<today YYYY-MM-DD>"'; do
  grep -Fq "$needle" <<<"$RENDERED_PROMPT" \
    && ok "shared prompt renders parameter: $needle" \
    || fail "shared prompt omitted parameter: $needle"
done

echo "=== test_capafy_ig_account_state: $P passed $F failed ==="
[ "$F" = 0 ]
