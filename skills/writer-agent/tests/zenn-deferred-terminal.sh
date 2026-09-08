#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
source "$ROOT/skills/writer-agent/tests/_exact8-fixture.sh"
WORKER="$ROOT/skills/writer-agent/scripts/zenn-deferred-worker.sh"
CONTROL="$ROOT/skills/writer-agent/scripts/zenn-deferred-control.py"
TMP="$(mktemp -d /tmp/zenn-terminal.XXXXXX)"
trap 'rm -rf -- "$TMP"' EXIT
RUN=run-terminal
LEDGER="$TMP/articles.jsonl"
ARTIFACT="$TMP/runs/$RUN/gates/zenn-deferred.json"
mkdir -p "$(dirname "$ARTIFACT")" "$TMP/repo/articles" "$TMP/bin"

printf '%s\n' '---' 'title: "Terminal title"' 'published: true' '---' >"$TMP/repo/articles/terminal-slug-1.md"
python3 "$CONTROL" create --artifact "$ARTIFACT" --markdown-file "$TMP/repo/articles/terminal-slug-1.md" --slug terminal-slug-1
exact8_init_state "$ROOT" "$TMP/runs/$RUN" "$LEDGER" "$RUN" topic-terminal terminal-slug-1
exact8_record_other_seven "$ROOT" "$TMP/runs/$RUN" "$LEDGER"
exact8_write_zenn_readback \
  "$TMP/runs/$RUN/gates/publication-state.json" "$TMP/zenn-readback.json" \
  terminal-slug-1 2026-07-22T03:00:00+00:00 \
  https://zenn.dev/writer-zenn/articles/terminal-slug-1
python3 "$CONTROL" handoff --test-allow-local-source --repo "$TMP/repo" --ledger "$LEDGER" \
  --run-id "$RUN" --artifact "$ARTIFACT" >/dev/null
python3 "$CONTROL" record --test-allow-local-source --repo "$TMP/repo" --ledger "$LEDGER" \
  --run-id "$RUN" --artifact "$ARTIFACT" \
  --published-at 2026-07-22T03:00:00+00:00 \
  --test-public-readback-json "$TMP/zenn-readback.json" \
  --live-url https://zenn.dev/writer-zenn/articles/terminal-slug-1 >/dev/null
jq --arg run "$RUN" '. + {
  status:"complete", run_id:$run, topic_id:"topic-terminal",
  completion_check:"passed", notification_status:"sent",
  completed_live_url:.live_url, heartbeat_at:"2026-07-21T00:00:00Z",
  completed_at:"2026-07-21T00:00:01Z", last_action:"complete"
}' "$ARTIFACT" >"$TMP/artifact.tmp" && mv "$TMP/artifact.tmp" "$ARTIFACT"
printf 'terminal-heartbeat\n' >"$TMP/heartbeat"

cat >"$TMP/bin/git" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GIT_CALLS"
exit 1
SH
cat >"$TMP/notify" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$NOTIFY_CALLS"
SH
chmod +x "$TMP/bin/git" "$TMP/notify"
: >"$TMP/git.calls"
BEFORE_ARTIFACT=$(shasum -a 256 "$ARTIFACT" | awk '{print $1}')
BEFORE_HEARTBEAT=$(shasum -a 256 "$TMP/heartbeat" | awk '{print $1}')

PATH="$TMP/bin:$PATH" GIT_CALLS="$TMP/git.calls" NOTIFY_CALLS="$TMP/notify.calls" \
  bash "$WORKER" --ledger "$LEDGER" --runs-root "$TMP/runs" --repo "$TMP/repo" \
  --expected-remote "$TMP/unreachable.git" --heartbeat "$TMP/heartbeat" --notify-bin "$TMP/notify" \
  --lock-file "$TMP/worker.lock" --publication-lock-dir "$TMP/publication.lockdir" \
  --backlog-state "$TMP/backlog.json" --log "$TMP/worker.log"

test "$(shasum -a 256 "$ARTIFACT" | awk '{print $1}')" = "$BEFORE_ARTIFACT"
test "$(shasum -a 256 "$TMP/heartbeat" | awk '{print $1}')" = "$BEFORE_HEARTBEAT"
test "$(jq -r .status "$ARTIFACT")" = complete
test ! -s "$TMP/git.calls"
test ! -e "$TMP/notify.calls"
rg -q 'scan complete' "$TMP/worker.log"

echo 'PASS: terminal exact-eight is immutable and performs no network or side effects'
