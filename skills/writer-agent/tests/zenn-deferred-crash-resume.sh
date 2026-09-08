#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
source "$ROOT/skills/writer-agent/tests/_exact8-fixture.sh"
CONTROL="$ROOT/skills/writer-agent/scripts/zenn-deferred-control.py"
WORKER="$ROOT/skills/writer-agent/scripts/zenn-deferred-worker.sh"
COMPLETE="$ROOT/skills/writer-agent/scripts/article-run-complete.py"
TMP="$(mktemp -d /tmp/zenn-crash-resume.XXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT
RUN=run-crash
LEDGER="$TMP/articles.jsonl"
ARTIFACT="$TMP/runs/$RUN/gates/zenn-deferred.json"
REPO="$TMP/repo"
mkdir -p "$(dirname "$ARTIFACT")" "$REPO/articles"
printf '%s\n' '---' 'title: "Crash title"' 'published: true' '---' >"$REPO/articles/crash-slug-1.md"
python3 "$CONTROL" create --artifact "$ARTIFACT" --markdown-file "$REPO/articles/crash-slug-1.md" --slug crash-slug-1
exact8_init_state "$ROOT" "$TMP/runs/$RUN" "$LEDGER" "$RUN" topic-1 crash-slug-1
exact8_record_other_seven "$ROOT" "$TMP/runs/$RUN" "$LEDGER"
exact8_write_zenn_readback \
  "$TMP/runs/$RUN/gates/publication-state.json" "$TMP/zenn-readback.json" \
  crash-slug-1 2026-07-22T03:00:00+00:00 \
  https://zenn.dev/writer-zenn/articles/crash-slug-1
python3 "$CONTROL" handoff --test-allow-local-source --repo "$REPO" --ledger "$LEDGER" --run-id "$RUN" --artifact "$ARTIFACT" >/dev/null
python3 "$CONTROL" record --test-allow-local-source --repo "$REPO" --ledger "$LEDGER" --run-id "$RUN" --artifact "$ARTIFACT" \
  --published-at 2026-07-22T03:00:00+00:00 \
  --test-public-readback-json "$TMP/zenn-readback.json" \
  --live-url https://zenn.dev/writer-zenn/articles/crash-slug-1 >/dev/null
test "$(jq -r .status "$ARTIFACT")" = live-recorded
test "$(python3 "$CONTROL" plan --test-allow-local-source --repo "$REPO" --ledger "$LEDGER" --run-id "$RUN" --artifact "$ARTIFACT" | jq -r .action)" = finalize

# Model a crash after ledger fsync but before the artifact state update.
jq '.status="pending"' "$ARTIFACT" >"$TMP/artifact.tmp" && mv "$TMP/artifact.tmp" "$ARTIFACT"
printf '#!/usr/bin/env bash\nexit 1\n' >"$TMP/fail-complete"; chmod +x "$TMP/fail-complete"
printf '#!/usr/bin/env bash\nexit 1\n' >"$TMP/fail-notify"; chmod +x "$TMP/fail-notify"
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$NOTIFY_CALLS"\n' >"$TMP/pass-notify"; chmod +x "$TMP/pass-notify"

bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" --repo "$REPO" \
  --test-allow-local-source --complete-bin "$TMP/fail-complete" --notify-bin "$TMP/pass-notify" \
  --heartbeat "$TMP/heartbeat" --lock-file "$TMP/worker.lock" \
  --publication-lock-dir "$TMP/publication.lockdir" \
  --backlog-state "$TMP/backlog.json" --log "$TMP/worker.log"
test "$(jq -r .status "$ARTIFACT")" = live-recorded
test "$(wc -l <"$LEDGER" | tr -d ' ')" -eq 8
test ! -e "$TMP/notify.calls"

bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" --repo "$REPO" \
  --test-allow-local-source --complete-bin "$COMPLETE" --notify-bin "$TMP/fail-notify" \
  --heartbeat "$TMP/heartbeat" --lock-file "$TMP/worker.lock" \
  --publication-lock-dir "$TMP/publication.lockdir" \
  --backlog-state "$TMP/backlog.json" --log "$TMP/worker.log"
test "$(jq -r .status "$ARTIFACT")" = live-recorded
test "$(jq -r .notification_status "$ARTIFACT")" = failed
test "$(wc -l <"$LEDGER" | tr -d ' ')" -eq 8

: >"$TMP/notify.calls"
NOTIFY_CALLS="$TMP/notify.calls" bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" --repo "$REPO" \
  --test-allow-local-source --complete-bin "$COMPLETE" --notify-bin "$TMP/pass-notify" \
  --heartbeat "$TMP/heartbeat" --lock-file "$TMP/worker.lock" \
  --publication-lock-dir "$TMP/publication.lockdir" \
  --backlog-state "$TMP/backlog.json" --log "$TMP/worker.log"
test "$(jq -r .status "$ARTIFACT")" = complete
test "$(jq -r .notification_status "$ARTIFACT")" = sent
test "$(jq -r .notification_message_id "$ARTIFACT")" = test-notify-bin
test "$(wc -l <"$LEDGER" | tr -d ' ')" -eq 8
test "$(wc -l <"$TMP/notify.calls" | tr -d ' ')" -eq 1

echo 'PASS: exact-eight reconciliation resumes safely across record/check/notify crash points'
