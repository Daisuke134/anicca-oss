#!/usr/bin/env bash
set -euo pipefail
export ARTICLE_TEST_ONLY=1

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
source "$ROOT/skills/writer-agent/tests/_exact8-fixture.sh"
CONTROL="$ROOT/skills/writer-agent/scripts/zenn-deferred-control.py"
WORKER="$ROOT/skills/writer-agent/scripts/zenn-deferred-worker.sh"
COMPLETE="$ROOT/skills/writer-agent/scripts/article-run-complete.py"
DAILY="$ROOT/skills/writer-agent/article-daily.sh"
WORKER_PY="$ROOT/skills/writer-agent/scripts/zenn-deferred-worker.py"
TMP="$(mktemp -d /tmp/article-zenn-deferred.XXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT
WORKER_STATE_ARGS=(--lock-file "$TMP/worker.lock" --backlog-state "$TMP/backlog.json")
SHARED_PUBLICATION_LOCK="$TMP/publication.lockdir"
WORKER_STATE_ARGS+=(--publication-lock-dir "$SHARED_PUBLICATION_LOCK")
RUN_ID="run-zenn"
LEDGER="$TMP/articles.jsonl"
RUN_DIR="$TMP/runs/$RUN_ID"
ARTIFACT="$RUN_DIR/gates/zenn-deferred.json"
mkdir -p "$RUN_DIR/gates" "$TMP/repo/articles" "$TMP/bin"

printf '%s\n' '---' 'title: "Target title"' 'published: true' '---' '# Body' >"$TMP/repo/articles/target-slug-1.md"
git init --bare "$TMP/remote.git" >/dev/null
git -C "$TMP/repo" init -b main >/dev/null
git -C "$TMP/repo" -c user.name=test -c user.email=test@example.test add articles/target-slug-1.md
git -C "$TMP/repo" -c user.name=test -c user.email=test@example.test commit -m seed >/dev/null
git -C "$TMP/repo" remote add origin "$TMP/remote.git"
git -C "$TMP/repo" push -u origin main >/dev/null
python3 "$CONTROL" create --artifact "$ARTIFACT" --markdown-file "$TMP/repo/articles/target-slug-1.md" --slug target-slug-1
exact8_init_state "$ROOT" "$RUN_DIR" "$LEDGER" "$RUN_ID" topic-1 target-slug-1
exact8_record_other_seven "$ROOT" "$RUN_DIR" "$LEDGER"
python3 - "$RUN_DIR/gates/publication-state.json" "$TMP/zenn-readback.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1], encoding="utf-8"))
pair = "zenn-article/ja"
assets = [item["sha256"] for item in state["media"]["body_assets"]]
urls = [f"https://assets.example/{index}.png" for index, _ in enumerate(assets)]
value = {
    "status": "live",
    "live_url": "https://zenn.dev/anicca/articles/target-slug-1",
    "verified": True,
    "public_id": "target-slug-1",
    "published_at": "2026-07-21T17:08:36+00:00",
    "stable_target": "target-slug-1",
    "artifact_sha256": state["drafts"]["ja"]["sha256"],
    "language": "ja",
    "content_verified": True,
    "asset_hashes": assets,
    "asset_urls": urls,
    "asset_proofs": [
        {
            "expected_sha256": expected,
            "remote_sha256": expected,
            "remote_url": url,
            "match_method": "exact-sha256",
        }
        for expected, url in zip(assets, urls)
    ],
    "asset_verified": True,
    "body_media_verified": True,
    "destination_identity": "anicca",
    "identity_verified": True,
    "identity_source": "zenn-username-scoped-api",
}
json.dump(value, open(sys.argv[2], "w", encoding="utf-8"))
PY
WORKER_STATE_ARGS+=(--test-allow-local-source --test-public-readback-json "$TMP/zenn-readback.json")

printf '%s\n' '{"articles":[{"slug":"old-slug","published_at":"2026-07-20T17:08:36+00:00"}]}' >"$TMP/api.json"
WAIT_JSON=$(python3 "$CONTROL" plan --ledger "$LEDGER" --run-id "$RUN_ID" --artifact "$ARTIFACT" \
  --repo "$TMP/repo" --api-json "$TMP/api.json" --now 2026-07-21T16:08:36+00:00)
test "$(printf '%s' "$WAIT_JSON" | jq -r .action)" = wait
test "$(printf '%s' "$WAIT_JSON" | jq -r .wait_seconds)" -eq 3600

RETRY_JSON=$(python3 "$CONTROL" plan --ledger "$LEDGER" --run-id "$RUN_ID" --artifact "$ARTIFACT" \
  --repo "$TMP/repo" --api-json "$TMP/api.json" --now 2026-07-21T17:08:36+00:00)
test "$(printf '%s' "$RETRY_JSON" | jq -r .action)" = retry

# The daily wrapper hands an eligible artifact to the durable queue and exits. It never
# sleeps for Zenn's rolling window and never starts another foreground model pass.
HANDOFF_JSON=$(python3 "$CONTROL" handoff --repo "$TMP/repo" --ledger "$LEDGER" --run-id "$RUN_ID" --artifact "$ARTIFACT")
test "$(printf '%s' "$HANDOFF_JSON" | jq -r .action)" = handed-off
test "$(jq -r .status "$ARTIFACT")" = pending
python3 - "$DAILY" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read()
completion = text.index("if ! pass_is_complete; then")
pending_owner = text.index("incomplete; durable pending worker owns", completion)
assert pending_owner > completion, "pending worker must own incomplete publication state"
assert text.count("run_model_pass") == 2, "one function definition plus one foreground call"
assert 'zenn-deferred-control.py" handoff' not in text[completion:], "daily wrapper must not duplicate pending-worker handoff"
assert 'zenn-deferred-retry.sh' not in text[completion:], "daily wrapper must not run the retry worker"
PY

# The dedicated worker is the sole Zenn owner, but it still shares the global publication
# exclusion boundary with the generic pending worker and the foreground daily pass.
mkdir "$SHARED_PUBLICATION_LOCK"
bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" --repo "$TMP/repo" \
  --expected-remote "$TMP/remote.git" "${WORKER_STATE_ARGS[@]}" \
  --api-json "$TMP/api.json" --now 2026-07-21T16:08:36+00:00 --log "$TMP/worker.log"
test "$(jq -r .status "$ARTIFACT")" = pending
rg -q 'shared publication lock is held' "$TMP/worker.log"
rmdir "$SHARED_PUBLICATION_LOCK"

# The independent worker scans every run directory and returns 0 while the window is closed.
# It must persist the retry time instead of sleeping in the launchd process.
bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" --repo "$TMP/repo" \
  --expected-remote "$TMP/remote.git" "${WORKER_STATE_ARGS[@]}" \
  --api-json "$TMP/api.json" --now 2026-07-21T16:08:36+00:00 --log "$TMP/worker.log"
test "$(jq -r .status "$ARTIFACT")" = pending
test "$(jq -r .retry_at "$ARTIFACT")" = '2026-07-21T17:08:36+00:00'
test "$(jq -r .last_action "$ARTIFACT")" = waiting
rg -q 'window closed.*pending retained' "$TMP/worker.log"
if rg -n '(^|[[:space:]])sleep([[:space:]]|$)' "$WORKER"; then
  echo 'FAIL: deferred worker must be one-shot and never sleep' >&2
  exit 1
fi

# API/network failures are retryable queue state, not a launchd failure.
printf 'not-json\n' >"$TMP/bad-api.json"
bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" --repo "$TMP/repo" \
  --expected-remote "$TMP/remote.git" "${WORKER_STATE_ARGS[@]}" \
  --api-json "$TMP/bad-api.json" --now 2026-07-21T16:18:36+00:00 --log "$TMP/worker.log"
test "$(jq -r .status "$ARTIFACT")" = pending
test "$(jq -r .last_action "$ARTIFACT")" = network-error
test "$(jq -r .last_error "$ARTIFACT")" != null

# A later healthy wait clears stale error text instead of leaving a false alarm.
bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" --repo "$TMP/repo" \
  --expected-remote "$TMP/remote.git" "${WORKER_STATE_ARGS[@]}" \
  --api-json "$TMP/api.json" --now 2026-07-21T16:20:00+00:00 --log "$TMP/worker.log"
test "$(jq -r .last_action "$ARTIFACT")" = waiting
test "$(jq -r 'has("last_error")' "$ARTIFACT")" = false

# A retrigger touches only the existing published:true source. A 403/reality failure is
# non-terminal: keep the artifact pending and return 0 so launchd can retry later.
cat >"$TMP/reality-gate.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
test "$1" = ssr
test "$2" = 'https://zenn.dev/anicca/articles/target-slug-1'
test "$3" = 'Target title'
test "$(cat "$REALITY_MODE")" = pass
printf 'VERDICT=PASS\n'
STUB
cat >"$TMP/notify.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$NOTIFY_CALLS"
STUB
chmod +x "$TMP/reality-gate.sh" "$TMP/notify.sh"
: >"$TMP/notify.calls"
printf 'fail\n' >"$TMP/reality.mode"
REALITY_MODE="$TMP/reality.mode" \
  NOTIFY_CALLS="$TMP/notify.calls" bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" \
  --repo "$TMP/repo" --expected-remote "$TMP/remote.git" "${WORKER_STATE_ARGS[@]}" \
  --api-json "$TMP/api.json" --now 2026-07-21T17:08:36+00:00 \
  --reality-gate "$TMP/reality-gate.sh" --complete-bin "$COMPLETE" \
  --heartbeat "$TMP/heartbeat" --notify-bin "$TMP/notify.sh" --log "$TMP/worker.log"
test "$(jq -r .status "$ARTIFACT")" = pending
test "$(jq -r .last_action "$ARTIFACT")" = reality-pending
test "$(jq -s --arg run "$RUN_ID" '[.[]|select(.run_id==$run and .published==true)]|length' "$LEDGER")" -eq 7
test "$(git --git-dir="$TMP/remote.git" rev-list --count main)" -eq 2

# Once Zenn exposes the same slug, verify it, append the eighth row once, mark the
# artifact terminal, update heartbeat, and send one completion notification.
printf '%s\n' '{"articles":[{"slug":"target-slug-1","published_at":"2026-07-21T17:08:36+00:00"}]}' >"$TMP/api.json"
printf 'pass\n' >"$TMP/reality.mode"
REALITY_MODE="$TMP/reality.mode" \
  NOTIFY_CALLS="$TMP/notify.calls" bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" \
  --repo "$TMP/repo" --expected-remote "$TMP/remote.git" "${WORKER_STATE_ARGS[@]}" \
  --api-json "$TMP/api.json" --now 2026-07-21T17:13:36+00:00 \
  --reality-gate "$TMP/reality-gate.sh" --complete-bin "$COMPLETE" \
  --heartbeat "$TMP/heartbeat" --notify-bin "$TMP/notify.sh" --log "$TMP/worker.log"
python3 "$COMPLETE" --ledger "$LEDGER" --run-id "$RUN_ID" --armed 1 \
  --publication-state "$RUN_DIR/gates/publication-state.json"
test "$(jq -r .status "$ARTIFACT")" = complete
test -f "$TMP/heartbeat"
test "$(wc -l <"$TMP/notify.calls" | tr -d ' ')" -eq 1

REALITY_MODE="$TMP/reality.mode" \
  NOTIFY_CALLS="$TMP/notify.calls" bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" \
  --repo "$TMP/repo" --expected-remote "$TMP/remote.git" "${WORKER_STATE_ARGS[@]}" \
  --api-json "$TMP/api.json" --now 2026-07-21T17:18:36+00:00 \
  --reality-gate "$TMP/reality-gate.sh" --complete-bin "$COMPLETE" \
  --heartbeat "$TMP/heartbeat" --notify-bin "$TMP/notify.sh" --log "$TMP/worker.log"
test "$(jq -s --arg run "$RUN_ID" '[.[]|select(.run_id==$run and .published==true)]|length' "$LEDGER")" -eq 8
test "$(git --git-dir="$TMP/remote.git" rev-list --count main)" -eq 2
test "$(wc -l <"$TMP/notify.calls" | tr -d ' ')" -eq 1

# The Python worker owns a non-blocking advisory lock across the whole scan.
rg -q 'fcntl\.LOCK_EX.*fcntl\.LOCK_NB' "$WORKER_PY"
echo 'PASS: async Zenn worker retains retryable failures, completes exact-eight once, and is lock-safe'
