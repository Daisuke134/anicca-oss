#!/usr/bin/env bash
# x-repost-digest.sh — send the daily digest through the same Telegram path the pass uses.
#
# Separate from the per-pass report on purpose: the pass answers "what did it just publish", this
# answers "is it working". Mixing them would bury the trend under the events.
#
# A failed send lands in the same backlog the next pass flushes, so a digest is never silently lost.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SKILL/../.." && pwd)"
STATE="${X_REPOST_STATE_DIR:-$HOME/loops/x-repost-en}"
PY=/opt/homebrew/bin/python3; [ -x "$PY" ] || PY=python3
WINDOW="${X_REPOST_DIGEST_WINDOW_HOURS:-24}"
TELEGRAM_SEND_TIMEOUT="${X_REPOST_TELEGRAM_SEND_TIMEOUT:-30}"
set -a
. "$HOME/.openclaw/.env" 2>/dev/null
set +a
TARGET="${TELEGRAM_ALERT_CHAT_ID:-}"

send_digest() {
  [ -n "$TARGET" ] || return 1
  local body="$1" idempotency_key response message_id
  idempotency_key="$(printf '%s' "$body" | shasum -a 256 | awk '{print $1}')"
  response="$(timeout "$TELEGRAM_SEND_TIMEOUT" openclaw message send \
    --channel telegram --target "$TARGET" --message "$body" --json \
    2>>"$STATE/digest.err")" || return 1
  printf '%s\n' "$response" >>"$STATE/digest.jsonl"
  message_id="$("$PY" -c 'import json,sys
def message_id(value):
    if isinstance(value, dict):
        for key in ("messageId", "message_id"):
            if value.get(key) is not None: return str(value[key])
        for child in value.values():
            found=message_id(child)
            if found: return found
    elif isinstance(value, list):
        for child in value:
            found=message_id(child)
            if found: return found
    return None
mid=message_id(json.loads(sys.argv[1])); print(mid or "")
raise SystemExit(0 if mid else 1)' "$response")" || return 1
  "$PY" - "$STATE/telegram-sent.jsonl" "$idempotency_key" "$message_id" <<'PYEOF'
import datetime, json, pathlib, sys
path, body_sha, message_id = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("body_sha256") == body_sha:
                raise SystemExit(0)
        except json.JSONDecodeError:
            pass
with path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"ts": datetime.datetime.now().astimezone().isoformat(),
        "body_sha256": body_sha, "message_id": message_id, "channel": "telegram"}) + "\n")
PYEOF
}

ALREADY_EVALUATED="$("$PY" - "$STATE/experiments.jsonl" <<'PYEOF'
import datetime, json, pathlib, sys
today = datetime.datetime.now().astimezone().date()
path = pathlib.Path(sys.argv[1])
seen = False
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            at = datetime.datetime.fromisoformat(json.loads(line).get("ts", "")).astimezone().date()
            seen = seen or at == today
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
print(seen)
PYEOF
)"

if [ "$ALREADY_EVALUATED" != "True" ]; then
  # Evaluate before reporting, so the digest carries today's verdict instead of yesterday's. The
  # evaluator refuses to move the knob on thin data and records that refusal.
  "$PY" "$SKILL/scripts/x_evaluate.py" --state "$STATE" \
    --window-hours "${X_REPOST_EXPERIMENT_WINDOW_HOURS:-48}" --apply >/dev/null 2>&1 \
    || echo "x-repost-digest: evaluation failed, reporting anyway" >&2

# Refill the well once a day. It used to run inside the hourly pass, which added a second long
# model call to every publish and pushed one pass past 35 minutes on an hourly schedule. The
# cooldown is fourteen days, so a seed a day is more than the loop can spend.
SEEDS="$STATE/seeds.jsonl"
AGENT_RUNNER="${AGENT_RUNNER_BIN:-$REPO_ROOT/runtime/agent-runner/agent_runner.py}"
AGENT_RUNNER_CONFIG="${AGENT_RUNNER_CONFIG:-$REPO_ROOT/runtime/agent-runner/config.json}"
MODEL_SCHEMA="$SKILL/config/model-output.schema.json"
{
  echo "以下は実在の出力だけを貼ったものだ。ここに書かれていることからのみ、"
  echo "**読んだ人が自分の状況に当てはめられる教訓** を ${X_REPOST_HARVEST_COUNT:-5}件 取り出せ。"
  echo
  echo "## 種の形（守らないと使い物にならない）"
  echo "- 書き出しは **一般化した教訓**。内部の事実はその根拠として後ろに1つ置くだけ。"
  echo "- **変数名・設定値・ラベル・ファイル名・コマンド名を書かない**。"
  echo "  悪い例: 「tone_weights を 1.0/1.0/1.0 → 0.5/1.5/1.0 に変更」「registered=False が173個」"
  echo "  良い例: 「測る前に決めた重みは、たいてい実測と逆を向いている」"
  echo "         「増やすのは一瞬で数えるのは後回しになる。気づくと自分でも把握できない数になる」"
  echo "- 数字は **1つまで**。意味が伝わるものだけ残す。"
  echo "- この機械でしか通じない話にしない。同じ失敗を他の人が別の道具でやりうる形にする。"
  echo "- 観点を変えて複数取る: 同じ材料でも「何が起きたか」「なぜ気づけなかったか」「一般化すると何か」は別の種。"
  echo "- 貼られていない数値・日付・固有名詞を作ったら失格。書けるものが少なければ少なくてよい。"
  echo; echo "## 既存の種"
  "$PY" "$SKILL/scripts/x_seeds.py" --seeds "$SEEDS" --available 2>/dev/null || echo "[]"
  echo; echo "## この loop の日次ダイジェスト（実測）"
  "$PY" "$SKILL/scripts/x_digest.py" --posted "$STATE/posted.jsonl" 2>/dev/null
  echo; echo "## この Mac の launchd 群（実測サマリ）"
  "$PY" "$SKILL/../../bin/launchd_inventory.py" --format json 2>/dev/null | "$PY" -c '
import json,sys,collections
try: a=json.load(sys.stdin)["agents"]
except Exception: sys.exit(0)
print("agents:",len(a))
print("registered:",dict(collections.Counter(x.get("registered") for x in a)))
print("state:",dict(collections.Counter(x.get("actual_state") for x in a)))' 2>/dev/null
  echo; echo "## 直近のパスのログ"
  tail -12 "${X_REPOST_LOG:-$HOME/.openclaw/logs/x-repost-pass.out.log}" 2>/dev/null
  echo; echo '## 出力（最後に JSON 配列だけを1つ）'
  echo '[{"fact":"...","measured_on":"YYYY-MM-DD","source":"どのセクションから取ったか"}]'
} >"$STATE/last-harvest-prompt.txt"

HARVEST_RUN="$STATE/evidence/digest-$(date +%Y%m%dT%H%M%S)"
if AGENT_RUNNER_CONFIG="$AGENT_RUNNER_CONFIG" "$PY" "$AGENT_RUNNER" \
     --task-class composition-agent --prompt-stdin --schema "$MODEL_SCHEMA" \
     --evidence-dir "$HARVEST_RUN" --task-label x-repost-digest --loop x-repost-digest \
     --workdir "$SKILL" --timeout-seconds "${X_REPOST_MODEL_TIMEOUT:-600}" \
     <"$STATE/last-harvest-prompt.txt" \
     >"$STATE/last-harvest.stdout" 2>"$STATE/last-harvest.err"; then
  "$PY" - "$HARVEST_RUN/summary.json" >"$STATE/last-harvest.json" <<'PYEOF'
import json, pathlib, sys
summary=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
value=json.loads(pathlib.Path(summary["result_path"]).read_text(encoding="utf-8"))
json.dump(value,sys.stdout,ensure_ascii=False)
PYEOF
  # The well drains far faster than it fills: the pass may publish up to twelve times a day and
  # each published post spends a seed, while this job used to add exactly one. Harvest several at
  # once, from different angles on the same material.
  ADDED=0
  while IFS= read -r seed; do
    [ -n "$seed" ] || continue
    printf '%s' "$seed" | "$PY" "$SKILL/scripts/x_seeds.py" --seeds "$SEEDS" --add \
      >>"$STATE/seeds.log" 2>&1 && ADDED=$((ADDED + 1))
  done < <("$PY" - "$STATE/last-harvest.json" <<'PYINNER'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(0)
rows = data if isinstance(data, list) else [data]
for row in rows:
    if isinstance(row, dict) and (row.get("fact") or "").strip():
        print(json.dumps(row, ensure_ascii=False))
PYINNER
)
  echo "x-repost-digest: harvested $ADDED seed(s)"
fi
else
  # The first delivery may have timed out after Telegram accepted it. The provider has no
  # idempotency key, so a same-day replay can create a duplicate even when telegram-sent.jsonl has
  # no receipt. Treat the daily evaluation itself as the at-most-once attempt fence.
  echo "x-repost-digest: today's evaluation already exists; no delivery replay"
  exit 0
fi

BODY="$("$PY" "$SKILL/scripts/x_digest.py" --posted "$STATE/posted.jsonl" --window-hours "$WINDOW")" || {
  echo "x-repost-digest: could not build the digest" >&2
  exit 1
}
MESSAGE="x-repost::: $BODY"
MESSAGE_SHA="$(printf '%s' "$MESSAGE" | shasum -a 256 | awk '{print $1}')"
if "$PY" - "$STATE/telegram-sent.jsonl" "$MESSAGE_SHA" <<'PYEOF'
import json, pathlib, sys
path, wanted = pathlib.Path(sys.argv[1]), sys.argv[2]
found = False
if path.exists():
    for line in path.read_text(encoding="utf-8").splitlines():
        try: found = found or json.loads(line).get("body_sha256") == wanted
        except json.JSONDecodeError: pass
raise SystemExit(0 if found else 1)
PYEOF
then
  echo "x-repost-digest: delivery already receipted"
  exit 0
fi

if send_digest "$MESSAGE"; then
  echo "x-repost-digest: sent"
  exit 0
fi
echo "x-repost-digest: ambiguous send; no retry or backlog enqueue" >&2
"$PY" -c 'import datetime,json,sys; open(sys.argv[1],"a",encoding="utf-8").write(json.dumps({"ts":datetime.datetime.now().astimezone().isoformat(),"body_sha256":sys.argv[2],"status":"ambiguous_no_retry"})+"\n")' \
  "$STATE/telegram-ambiguous.jsonl" "$MESSAGE_SHA"
exit 1
