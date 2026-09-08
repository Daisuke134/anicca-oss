#!/usr/bin/env bash
# Wrapper-side judge broker for Writer Agent.
#
# The bounded agent sandbox cannot start another provider process, so nested
# judge/vision calls write request files into the run-scoped state tree. This
# broker runs OUTSIDE the sandbox (started by the daily/resume wrappers),
# executes each request through the single model boundary, and writes the
# response back. It never rewrites prompts and never handles agent mode.
set -uo pipefail

RUN_DIR="${1:?usage: judge-broker.sh <run-dir>}"
BROKER_DIR="$RUN_DIR/gates/judge-broker"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${LIFE_MANAGER_REPO:-$(cd "$SCRIPT_DIR/../../.." && pwd -P)}"
RUNNER="${ARTICLE_MODEL_RUNNER:-$SCRIPT_DIR/model-runner.sh}"
POLL_SECONDS="${ARTICLE_JUDGE_BROKER_POLL_SECONDS:-1}"

mkdir -p "$BROKER_DIR/requests" "$BROKER_DIR/responses" "$BROKER_DIR/done"

# Env-independent discovery: register this broker at a fixed state-root path
# so the model-runner client can route even when an agent subshell dropped
# every ARTICLE_* variable (observed repeatedly on 2026-07-25).
STATE_ROOT="${ARTICLE_MODEL_STATE_ROOT:-${ARTICLE_STATE_DIR:-${ARTICLE_SKILL_DIR:-$SCRIPT_DIR/..}/state}}"
REGISTRY="$STATE_ROOT/.judge-broker-active"
mkdir -p "$STATE_ROOT"
# The registry deliberately outlives the broker for post-mortem discovery.
# Clients require both a fresh heartbeat and this broker's live PID, so an
# abrupt parent exit cannot create a 180-second false-live routing window.
printf '%s\n' "$BROKER_DIR" > "$REGISTRY.tmp" && mv "$REGISTRY.tmp" "$REGISTRY"
PID_FILE="$BROKER_DIR/pid"
printf '%s\n' "$$" > "$PID_FILE.tmp" && mv "$PID_FILE.tmp" "$PID_FILE"
cleanup_pid() {
  [ -f "$PID_FILE" ] || return 0
  [ "$(cat "$PID_FILE" 2>/dev/null)" = "$$" ] && rm -f "$PID_FILE"
}
trap cleanup_pid EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

# Startup GC: a request much older than any live client wait predates this
# broker (a wedged predecessor stranded 23 on 2026-07-25). Serving corpses
# burns minutes on judgments nobody reads; age decides, so a request written
# moments before startup is still served normally.
STARTUP_NOW="$(date +%s)"
for stale in "$BROKER_DIR"/requests/*.json; do
  [ -f "$stale" ] || continue
  stale_age=$(( STARTUP_NOW - $(stat -f %m "$stale" 2>/dev/null || echo "$STARTUP_NOW") ))
  [ "$stale_age" -lt 120 ] && continue
  stale_id="$(basename "$stale" .json)"
  mv "$stale" "$BROKER_DIR/done/$stale_id.json.stale" 2>/dev/null || true
  mv "$BROKER_DIR/requests/$stale_id.prompt" \
    "$BROKER_DIR/done/$stale_id.prompt.stale" 2>/dev/null || true
done

serve_one() {
  local request="$1"
  local id mode image rc
  id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$request" 2>/dev/null)" || return 0
  mode="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["mode"])' "$request" 2>/dev/null)" || return 0
  image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("image") or "")' "$request" 2>/dev/null)" || image=""
  case "$mode" in
    judge|vision) ;;
    *)
      mv "$request" "$BROKER_DIR/done/" 2>/dev/null || true
      return 0
      ;;
  esac
  local -a args
  args=("$mode" --prompt-file "$BROKER_DIR/requests/$id.prompt")
  [ "$mode" = "vision" ] && [ -n "$image" ] && args+=(--image "$image")
  # Concurrent codex instances intermittently fail app-server init while the
  # agent process is running (observed 2026-07-25: ~50 OK, a few EPERM in the
  # same run). That transient class gets a bounded retry so a single blip
  # cannot fail-close a publication-blocking safety gate.
  local tries=0 max_tries=3 err_snap
  err_snap="$BROKER_DIR/.serve-$id.err"
  # A hung provider process must never wedge the whole broker (observed
  # 2026-07-25 17:42: one stuck judge stranded a 23-request backlog and
  # every later safety gate). Bound each execution; timeout is a failed
  # response the gate can classify, never a silent stall.
  local exec_timeout timeout_bin
  # Keep one stalled judge below the five-minute launchd cadence. A timeout
  # is returned as a failed gate; it never fabricates a verdict.
  exec_timeout="${ARTICLE_JUDGE_BROKER_EXEC_TIMEOUT:-120}"
  timeout_bin="$(command -v gtimeout || command -v timeout || true)"
  while :; do
    tries=$((tries + 1))
    touch "$BROKER_DIR/heartbeat"
    set +e
    if [ -n "$timeout_bin" ]; then
      ARTICLE_NESTED_SANDBOX= ARTICLE_JUDGE_BROKER_SERVER=1 \
        "$timeout_bin" "$exec_timeout" "$RUNNER" "${args[@]}" \
        >"$BROKER_DIR/responses/$id.out" 2>"$err_snap"
    else
      ARTICLE_NESTED_SANDBOX= ARTICLE_JUDGE_BROKER_SERVER=1 \
        python3 "$REPO_ROOT/runtime/loop/bounded-exec.py" "$exec_timeout" "$RUNNER" "${args[@]}" \
        >"$BROKER_DIR/responses/$id.out" 2>"$err_snap"
    fi
    rc=$?
    set -e
    cat "$err_snap" >>"$BROKER_DIR/broker.err"
    if [ "$rc" -eq 0 ] || [ "$tries" -ge "$max_tries" ]; then
      break
    fi
    if ! grep -qE "app-server client|PATH aliases" "$err_snap"; then
      break
    fi
    sleep "${ARTICLE_JUDGE_BROKER_RETRY_SLEEP:-5}"
  done
  rm -f "$err_snap"
  printf '{"id":"%s","rc":%d}\n' "$id" "$rc" \
    >"$BROKER_DIR/responses/.tmp-$id"
  mv "$BROKER_DIR/responses/.tmp-$id" "$BROKER_DIR/responses/$id.json"
  mv "$request" "$BROKER_DIR/done/" 2>/dev/null || true
  rm -f "$BROKER_DIR/done/$id.prompt" 2>/dev/null || true
}

while :; do
  [ -f "$BROKER_DIR/stop" ] && exit 0
  # Liveness marker: a fresh heartbeat makes the model-runner client route
  # every non-agent call through this broker, env markers notwithstanding.
  touch "$BROKER_DIR/heartbeat"
  served=0
  for request in "$BROKER_DIR"/requests/*.json; do
    [ -f "$request" ] || continue
    serve_one "$request"
    served=1
  done
  # Test hook: exit cleanly once the queue drains when stop-after-idle exists.
  if [ -f "$BROKER_DIR/stop-after-idle" ] && [ "$served" = 0 ] \
    && ! ls "$BROKER_DIR"/requests/*.json >/dev/null 2>&1; then
    exit 0
  fi
  sleep "$POLL_SECONDS"
done
