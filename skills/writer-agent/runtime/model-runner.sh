#!/usr/bin/env bash
# One model process boundary for Writer Agent.
set -uo pipefail

# launchd jobs run with a minimal environment that omits USER; the claude CLI
# reads its stored OAuth credentials only when USER is set, so a launchd-run
# claude call fails "Not logged in" even though the account is fully logged
# in (measured 2026-09-04). codex does not read USER for auth, so this is
# scoped to the value claude needs.
: "${USER:=$(id -un)}"
export USER

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPPORT="$SCRIPT_DIR/model-runner-support.py"
MODE="${1:-}"
[ "$#" -gt 0 ] && shift

PROMPT_FILE=""
IMAGE_FILE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --prompt-file)
      [ "$#" -ge 2 ] || { echo "model-runner: --prompt-file requires a value" >&2; exit 64; }
      PROMPT_FILE="$2"
      shift 2
      ;;
    --image)
      [ "$#" -ge 2 ] || { echo "model-runner: --image requires a value" >&2; exit 64; }
      IMAGE_FILE="$2"
      shift 2
      ;;
    *)
      echo "model-runner: unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

case "$MODE" in
  agent|judge|vision|repair) ;;
  *) echo "model-runner: mode must be agent, judge, vision, or repair" >&2; exit 64 ;;
esac

# SSOT §9.3.1 H2. `agent` is write-capable but has no event stream (the C1
# session surface below is deliberately off for it, so the daily writing path
# keeps a byte-identical command line), and `judge` has the event stream but is
# read-only and offline. A bounded repair needs both, so it gets its own mode
# rather than widening either existing one.
#
# The cage is measured, not assumed. On codex-cli 0.145.0, 2026-08-07:
# `--sandbox workspace-write` alone still permitted a write to a path under
# `/tmp`, because `/tmp` and `$TMPDIR` are writable roots by default. With both
# exclusions set, writes to `$HOME` and to `/tmp` both returned "operation not
# permitted", `curl` failed name resolution, and `git commit` in a linked
# worktree failed on `.git/worktrees/<name>/index.lock`. That is exactly the
# boundary this mode needs: it may edit files in its own workspace and nothing
# else, and it has no network at all, so it cannot publish, post, submit, or
# send. Fetching public documents is done outside the model by the caller.
if [ "$MODE" = "repair" ]; then
  [ -n "${ARTICLE_REPAIR_WORKSPACE:-}" ] || {
    echo "model-runner: repair mode requires ARTICLE_REPAIR_WORKSPACE" >&2; exit 64; }
  [ -d "${ARTICLE_REPAIR_WORKSPACE}" ] || {
    echo "model-runner: repair workspace is not a directory" >&2; exit 64; }
  case "$ARTICLE_REPAIR_WORKSPACE" in
    /*) ;;
    *) echo "model-runner: repair workspace must be an absolute path" >&2; exit 64 ;;
  esac
  # Write capability and a machine-readable outcome are one mode, never
  # separable: a repair whose failure class cannot be read is a repair whose
  # result is guessed.
  [ -n "${ARTICLE_CODEX_EVENTS_FILE:-}" ] || {
    echo "model-runner: repair mode requires ARTICLE_CODEX_EVENTS_FILE" >&2; exit 64; }
  [ -n "${ARTICLE_CODEX_LAST_MESSAGE_FILE:-}" ] || {
    echo "model-runner: repair mode requires ARTICLE_CODEX_LAST_MESSAGE_FILE" >&2; exit 64; }
fi

# Only judge and vision are brokered. `agent` starts its own provider, and
# `repair` runs in a cage that would deny a nested provider process anyway.
BROKER_MODE=0
case "$MODE" in
  judge|vision) BROKER_MODE=1 ;;
esac
PROMPT_STDIN_FILE=""
if [ "$PROMPT_FILE" = "-" ]; then
  PROMPT_STDIN_FILE="$(mktemp "${TMPDIR:-/tmp}/article-model-prompt.XXXXXX")"
  chmod 600 "$PROMPT_STDIN_FILE"
  cat >"$PROMPT_STDIN_FILE"
  PROMPT_FILE="$PROMPT_STDIN_FILE"
  trap 'rm -f -- "$PROMPT_STDIN_FILE"' EXIT
fi
[ -f "$PROMPT_FILE" ] || { echo "model-runner: prompt file is missing" >&2; exit 64; }
if [ "$MODE" = "vision" ] && [ ! -f "$IMAGE_FILE" ]; then
  echo "model-runner: vision mode requires an existing --image" >&2
  exit 64
fi

# Judge broker client: a nested judge/vision call inside the bounded agent
# sandbox cannot start another provider process (codex app-server init is
# denied). It hands the prompt to the unsandboxed wrapper broker through the
# run-scoped state tree and waits for the response; timeout is a retryable
# provider failure, never a fabricated verdict.
# Routing triggers on EITHER the env marker OR a live broker heartbeat: an
# agent clearing ARTICLE_NESTED_SANDBOX (observed 2026-07-25) cannot bypass
# the broker while one is serving this run. The broker's own server-side
# invocation sets ARTICLE_JUDGE_BROKER_SERVER=1 and is never re-routed.
BROKER_HEARTBEAT_LIVE=0
DISCOVERED_BROKER_DIR=""
BROKER_FROM_REGISTRY=0
broker_pid_live() {
  local broker_dir="$1" pid
  [ -f "$broker_dir/pid" ] || return 1
  pid="$(cat "$broker_dir/pid" 2>/dev/null)"
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$pid" -gt 1 ] 2>/dev/null || return 1
  kill -0 "$pid" 2>/dev/null
}
if [ "${ARTICLE_JUDGE_BROKER_SERVER:-}" != "1" ]; then
  REGISTRY_STATE_ROOT="${ARTICLE_MODEL_STATE_ROOT:-${ARTICLE_STATE_DIR:-${ARTICLE_SKILL_DIR:-$SCRIPT_DIR/..}/state}}"
  if [ -n "${ARTICLE_RUN_DIR:-}" ]; then
    DISCOVERED_BROKER_DIR="${ARTICLE_RUN_DIR}/gates/judge-broker"
  else
    # Env-independent fallback: the broker registers itself at a fixed
    # state-root path, so routing survives agent subshells that drop env.
    if [ -f "$REGISTRY_STATE_ROOT/.judge-broker-active" ]; then
      DISCOVERED_BROKER_DIR="$(cat "$REGISTRY_STATE_ROOT/.judge-broker-active" 2>/dev/null)"
      BROKER_FROM_REGISTRY=1
    fi
  fi
  # A registry is runtime state owned by this exact state root. Ignore a
  # pointer outside state/runs (including a pytest temp broker): otherwise a
  # fresh external heartbeat can route production judges into a broker that
  # no production process serves, causing a 900-second blind wait.
  if [ "$BROKER_FROM_REGISTRY" = "1" ] && [ -n "$DISCOVERED_BROKER_DIR" ]; then
    BROKER_REGISTRY_VALID="$(python3 -c '
from pathlib import Path
import sys
root = (Path(sys.argv[1]) / "runs").resolve()
candidate = Path(sys.argv[2]).resolve()
valid = (
    candidate.name == "judge-broker"
    and candidate.parent.name == "gates"
    and candidate.is_relative_to(root)
)
print("1" if valid else "0")
' "$REGISTRY_STATE_ROOT" "$DISCOVERED_BROKER_DIR" 2>/dev/null || echo 0)"
    [ "$BROKER_REGISTRY_VALID" = "1" ] || DISCOVERED_BROKER_DIR=""
  fi
  if [ -n "$DISCOVERED_BROKER_DIR" ] \
    && [ -f "$DISCOVERED_BROKER_DIR/heartbeat" ] \
    && broker_pid_live "$DISCOVERED_BROKER_DIR"; then
    HEARTBEAT_AGE=$(( $(date +%s) - $(stat -f %m "$DISCOVERED_BROKER_DIR/heartbeat" 2>/dev/null || echo 0) ))
    [ "$HEARTBEAT_AGE" -lt 180 ] && BROKER_HEARTBEAT_LIVE=1
  fi
  # A gate may intentionally isolate its attempt receipts under a child
  # ARTICLE_RUN_DIR. If that child has no serving broker, reuse the live
  # run-scoped broker registered by the parent instead of writing a request
  # into an unserved directory and waiting until timeout.
  if [ "$BROKER_HEARTBEAT_LIVE" != "1" ] \
    && [ -f "$REGISTRY_STATE_ROOT/.judge-broker-active" ]; then
    REGISTRY_BROKER_DIR="$(cat "$REGISTRY_STATE_ROOT/.judge-broker-active" 2>/dev/null)"
    REGISTRY_BROKER_VALID="$(python3 -c '
from pathlib import Path
import sys
root = (Path(sys.argv[1]) / "runs").resolve()
candidate = Path(sys.argv[2]).resolve()
valid = (
    candidate.name == "judge-broker"
    and candidate.parent.name == "gates"
    and candidate.is_relative_to(root)
)
print("1" if valid else "0")
' "$REGISTRY_STATE_ROOT" "$REGISTRY_BROKER_DIR" 2>/dev/null || echo 0)"
    if [ "$REGISTRY_BROKER_VALID" = "1" ] \
      && [ -f "$REGISTRY_BROKER_DIR/heartbeat" ] \
      && broker_pid_live "$REGISTRY_BROKER_DIR"; then
      REGISTRY_HEARTBEAT_AGE=$(( $(date +%s) - $(stat -f %m "$REGISTRY_BROKER_DIR/heartbeat" 2>/dev/null || echo 0) ))
      if [ "$REGISTRY_HEARTBEAT_AGE" -lt 180 ]; then
        DISCOVERED_BROKER_DIR="$REGISTRY_BROKER_DIR"
        BROKER_FROM_REGISTRY=1
        BROKER_HEARTBEAT_LIVE=1
      fi
    fi
  fi
fi
if [ "$BROKER_MODE" = "1" ] && [ "${ARTICLE_JUDGE_BROKER_SERVER:-}" != "1" ] \
  && [ "$BROKER_HEARTBEAT_LIVE" = "1" ]; then
  if [ -z "$DISCOVERED_BROKER_DIR" ]; then
    echo "model-runner: nested sandbox judge found no broker (no ARTICLE_RUN_DIR and no active registry)" >&2
    exit 64
  fi
  BROKER_DIR="$DISCOVERED_BROKER_DIR"
  mkdir -p "$BROKER_DIR/requests" "$BROKER_DIR/responses"
  REQUEST_ID="$(date +%s%N).$$"
  cp "$PROMPT_FILE" "$BROKER_DIR/requests/$REQUEST_ID.prompt"
  REQUEST_TMP="$BROKER_DIR/requests/.tmp-$REQUEST_ID"
  printf '{"id":"%s","mode":"%s","image":"%s"}\n' \
    "$REQUEST_ID" "$MODE" "${IMAGE_FILE:-}" >"$REQUEST_TMP"
  mv "$REQUEST_TMP" "$BROKER_DIR/requests/$REQUEST_ID.json"
  BROKER_DEADLINE=$(( $(date +%s) + ${ARTICLE_JUDGE_BROKER_TIMEOUT:-900} ))
  while [ ! -f "$BROKER_DIR/responses/$REQUEST_ID.json" ]; do
    if [ "$(date +%s)" -ge "$BROKER_DEADLINE" ]; then
      echo "model-runner: judge broker timed out" >&2
      exit 75
    fi
    sleep 1
  done
  BROKER_RC="$(python3 -c 'import json,sys; print(int(json.load(open(sys.argv[1])).get("rc", 75)))' \
    "$BROKER_DIR/responses/$REQUEST_ID.json" 2>/dev/null || echo 75)"
  cat "$BROKER_DIR/responses/$REQUEST_ID.out" 2>/dev/null
  exit "$BROKER_RC"
fi
if [ "$BROKER_MODE" = "1" ] && [ "${ARTICLE_JUDGE_BROKER_SERVER:-}" != "1" ] \
  && [ "${ARTICLE_NESTED_SANDBOX:-}" = "1" ]; then
  echo "model-runner: nested sandbox judge found no live broker" >&2
  exit 64
fi

PROVIDER="${ARTICLE_PROVIDER:-claude}"
case "$PROVIDER" in
  auto|codex|claude) ;;
  *) echo "model-runner: ARTICLE_PROVIDER must be auto, codex, or claude" >&2; exit 64 ;;
esac

# The repair cage is a codex sandbox policy. The claude branch below has no
# equivalent, so a repair must never reach it: pinning the provider here means
# `CANDIDATES` never contains `claude` and `invoke_provider` is only ever
# called with `codex` for this mode.
if [ "$MODE" = "repair" ]; then
  case "$PROVIDER" in
    auto|codex) PROVIDER="codex" ;;
    *) echo "model-runner: repair mode requires the codex provider" >&2; exit 64 ;;
  esac
fi

MODEL_ROOT="${ARTICLE_MODEL_ROOT:-${ARTICLE_SKILL_DIR:-$SCRIPT_DIR/..}}"
MODEL_STATE_ROOT="${ARTICLE_MODEL_STATE_ROOT:-${ARTICLE_STATE_DIR:-$MODEL_ROOT/state}}"
RUN_ID="${ARTICLE_RUN_ID:-unknown}"
HEALTH_FILE="${ARTICLE_PROVIDER_HEALTH:-$MODEL_STATE_ROOT/provider-health.json}"
MODEL_LOG="${ARTICLE_MODEL_LOG:-$MODEL_STATE_ROOT/model-runner.log}"
COOLDOWN_SECONDS="${ARTICLE_PROVIDER_COOLDOWN_SECONDS:-300}"
CODEX_BIN="${ARTICLE_CODEX_BIN:-$(command -v codex 2>/dev/null || true)}"
CLAUDE_BIN="${ARTICLE_CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
TEMP_FAILURE=75
MODEL_ROLE="${ARTICLE_MODEL_ROLE:-terra}"
CODEX_MODEL="gpt-5.6-terra"
CODEX_EFFORT="${ARTICLE_MODEL_REASONING_EFFORT:-medium}"
CODEX_PROVIDER_ID="${ARTICLE_CODEX_PROVIDER_ID:-openai}"
CODEX_PROVIDER_BASE_URL="${ARTICLE_CODEX_PROVIDER_BASE_URL:-}"
CODEX_PROVIDER_ENV_KEY="${ARTICLE_CODEX_PROVIDER_ENV_KEY:-}"
CODEX_PROVIDER_API_KEY_SOURCE="${ARTICLE_CODEX_PROVIDER_API_KEY_SOURCE:-}"
CODEX_PROVIDER_CONFIG="${ARTICLE_CLIPROXY_CONFIG:-}"

load_codex_provider_key() {
  set +x
  case "$CODEX_PROVIDER_API_KEY_SOURCE" in
    "") ;;
    cliproxyapi)
      [ "$CODEX_PROVIDER_ENV_KEY" = "CLIPROXY_API_KEY" ] || return 1
      local provider_key="${ARTICLE_CODEX_PROVIDER_API_KEY:-}"
      if [ -z "$provider_key" ]; then
        [ -n "$CODEX_PROVIDER_CONFIG" ] && [ -r "$CODEX_PROVIDER_CONFIG" ] || return 1
        provider_key="$(awk '/^api-keys:/{in_keys=1; next} in_keys && /- "/{gsub(/.*- "/, ""); gsub(/".*/, ""); print; exit}' "$CODEX_PROVIDER_CONFIG")"
      fi
      [ -n "$provider_key" ] || return 1
      export CLIPROXY_API_KEY="$provider_key"
      unset provider_key
      ;;
    *) return 1 ;;
  esac
}

configure_codex_provider() {
  [ "$CODEX_PROVIDER_ID" = "openai" ] && return 0
  case "$CODEX_PROVIDER_ID" in
    *[!A-Za-z0-9_]*|'') return 1 ;;
  esac
  [[ "$CODEX_PROVIDER_BASE_URL" =~ ^https://[^[:space:]]+/v1$|^http://127\.0\.0\.1:[0-9]+/v1$ ]] || return 1
  [ "$CODEX_PROVIDER_ENV_KEY" = "CLIPROXY_API_KEY" ] || return 1
  load_codex_provider_key
}

case "$MODEL_ROLE" in
  terra)
    ;;
  sol-audit)
    SOL_RECEIPT="${ARTICLE_SOL_TRIGGER_RECEIPT:-}"
    if [ -z "$SOL_RECEIPT" ] || [ ! -f "$SOL_RECEIPT" ]; then
      echo "model-runner: sol-audit requires ARTICLE_SOL_TRIGGER_RECEIPT" >&2
      exit 64
    fi
    if ! jq -e --arg run_id "$RUN_ID" '
      .schema_version == 1
      and .run_id == $run_id
      and (.artifact_id | type == "string" and length > 0)
      and (.article_sha256 | type == "string" and test("^[0-9a-fA-F]{64}$"))
      and (.requested_reasoning_effort == "medium" or .requested_reasoning_effort == "high")
      and (.trigger as $trigger | [
        "medical", "legal", "financial", "high_value_submission",
        "new_topic_class", "quality_sample", "strategy_promotion"
      ] | index($trigger) != null)
    ' "$SOL_RECEIPT" >/dev/null 2>&1; then
      echo "model-runner: invalid sol trigger receipt" >&2
      exit 64
    fi
    SOL_CLAIM_DIR="${SOL_RECEIPT}.claim"
    if ! mkdir "$SOL_CLAIM_DIR" 2>/dev/null; then
      echo "model-runner: sol trigger receipt already claimed" >&2
      exit 78
    fi
    shasum -a 256 "$SOL_RECEIPT" | awk '{print $1}' >"$SOL_CLAIM_DIR/receipt.sha256"
    CODEX_MODEL="gpt-5.6-sol"
    CODEX_EFFORT="$(jq -r '.requested_reasoning_effort' "$SOL_RECEIPT")"
    PROVIDER="codex"
    ;;
  *)
    echo "model-runner: ARTICLE_MODEL_ROLE must be terra or sol-audit" >&2
    exit 64
    ;;
esac

# Production Writer passes use the repository-global provider/profile router.
# Explicit fake binaries keep the executable contract harness deterministic.
# Every production mode, including repair/session, otherwise delegates.
if [ -z "${ARTICLE_CODEX_BIN:-}" ] \
  && [ -z "${ARTICLE_CLAUDE_BIN:-}" ]; then
  SHARED_ADAPTER="$SCRIPT_DIR/shared-model-runner.py"
  SHARED_ARGS=("$MODE" --prompt-file "$PROMPT_FILE")
  [ "$MODE" = "vision" ] && SHARED_ARGS+=(--image "$IMAGE_FILE")
  exec python3 "$SHARED_ADAPTER" "${SHARED_ARGS[@]}"
fi

mkdir -p "$(dirname "$MODEL_LOG")" "$(dirname "$HEALTH_FILE")" "$MODEL_STATE_ROOT"

log_event() {
  printf '%s run_id=%s provider=%s mode=%s status=%s%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RUN_ID" "$1" "$MODE" "$2" "${3:+ $3}" \
    >>"$MODEL_LOG"
}

record_health() {
  python3 "$SUPPORT" record \
    --file "$HEALTH_FILE" \
    --provider "$1" \
    --mode "$MODE" \
    --status "$2" \
    --error-class "${3:-}" \
    --cooldown "$COOLDOWN_SECONDS" \
    --run-id "$RUN_ID"
}

is_eligible() {
  python3 "$SUPPORT" eligible \
    --file "$HEALTH_FILE" --provider "$1" --mode "$MODE" >/dev/null 2>&1
}

provider_binary() {
  if [ "$1" = "codex" ]; then
    printf '%s' "$CODEX_BIN"
  else
    printf '%s' "$CLAUDE_BIN"
  fi
}

# Opt-in automation surface for the repair investigation only (SSOT §9.4 C1/C3).
# OFF unless the caller sets ARTICLE_CODEX_EVENTS_FILE, so the daily writing,
# editorial judge, and vision paths keep byte-identical command lines and stay
# --ephemeral. When ON, the session is persisted so it can be resumed, the
# event stream is machine-readable, and the final answer is schema-bound.
CODEX_SESSION_MODE=0
if [ -n "${ARTICLE_CODEX_EVENTS_FILE:-}" ] && [ "$MODE" != "agent" ]; then
  CODEX_SESSION_MODE=1
fi

invoke_provider() {
  local provider="$1"
  local binary prompt sandbox raw_out raw_err safe_out safe_err rc provider_config_rc errexit_was_set
  binary="$(provider_binary "$provider")"
  prompt="$(<"$PROMPT_FILE")"
  if [ "$CODEX_SESSION_MODE" = "1" ] && [ "$provider" = "codex" ]; then
    # codex flushes each JSONL event as it is produced, so writing the stream
    # straight to the caller's file means a budget SIGKILL still leaves every
    # event that landed. Buffering it in a temp file and emitting it only after
    # the child exits is what made the 2026-08-07 timeout lose everything.
    raw_out="$ARTICLE_CODEX_EVENTS_FILE"
    mkdir -p "$(dirname "$raw_out")"
    : >"$raw_out"
  else
    raw_out="$(mktemp "${TMPDIR:-/tmp}/article-model-out.XXXXXX")"
  fi
  raw_err="$(mktemp "${TMPDIR:-/tmp}/article-model-err.XXXXXX")"
  safe_out="$(mktemp "${TMPDIR:-/tmp}/article-model-safe-out.XXXXXX")"
  safe_err="$(mktemp "${TMPDIR:-/tmp}/article-model-safe-err.XXXXXX")"

  local -a command
  if [ "$provider" = "codex" ]; then
    # Dais 2026-07-25: the agent runs WITHOUT a sandbox on this local Mac.
    # The workspace-write cage caused every judge/publish failure class today
    # (nested app-server EPERM, blocked DNS, Telegram identity EPERM) and the
    # local machine is the trust boundary. Source discipline stays enforced
    # by the prompt's hard boundaries and the identity/conscience gates.
    sandbox="read-only"
    local working_root="$MODEL_ROOT"
    if [ "$MODE" = "agent" ]; then
      sandbox="danger-full-access"
      working_root="$MODEL_ROOT"
    elif [ "$MODE" = "repair" ]; then
      # Narrow on purpose. `danger-full-access` with $HOME writable would let an
      # unattended repair worker reach credentials, other loops' state, and the
      # live checkout. The workspace is the only writable root.
      sandbox="workspace-write"
      working_root="$ARTICLE_REPAIR_WORKSPACE"
    fi
    if [ "$CODEX_SESSION_MODE" = "1" ]; then
      # No --ephemeral: the session must persist to be resumable (C3).
      command=(
        "$binary" exec
        --model "$CODEX_MODEL"
        -c "model_reasoning_effort=\"$CODEX_EFFORT\""
        --sandbox "$sandbox"
        -C "$working_root"
      )
    else
      command=(
        "$binary" exec --ephemeral
        --model "$CODEX_MODEL"
        -c "model_reasoning_effort=\"$CODEX_EFFORT\""
        --sandbox "$sandbox"
        -C "$working_root"
      )
    fi
    if [ "$MODE" = "repair" ]; then
      # Measured 2026-08-07 on codex-cli 0.145.0: without both exclusions,
      # workspace-write still allows writes under /tmp and $TMPDIR, which is a
      # channel out of the workspace. network_access is stated rather than left
      # to a default so a config change cannot silently open egress -- and
      # --ignore-user-config below means ~/.codex/config.toml cannot widen any
      # of this either.
      command+=(
        -c "sandbox_workspace_write.exclude_slash_tmp=true"
        -c "sandbox_workspace_write.exclude_tmpdir_env_var=true"
        -c "sandbox_workspace_write.network_access=false"
      )
    fi
    # The Writer prompt is the only orchestration surface.  Do not inherit
    # the operator's Codex MCP/skill/rules graph: that graph started unrelated
    # CodeGraph, CUA, and Premiere services inside a daily canary and held the
    # owner fence without reaching a publication boundary.
    command+=(--ignore-user-config --ignore-rules)
    [ "$MODE" = "agent" ] && command+=(--add-dir "$HOME")
    if [ "$CODEX_SESSION_MODE" = "1" ]; then
      # C1: events as JSONL and the final message in its own file, so the
      # outcome is read from the stream instead of a binary exit status.
      command+=(--json -o "${ARTICLE_CODEX_LAST_MESSAGE_FILE:?session mode requires ARTICLE_CODEX_LAST_MESSAGE_FILE}")
      [ -n "${ARTICLE_CODEX_OUTPUT_SCHEMA:-}" ] \
        && command+=(--output-schema "$ARTICLE_CODEX_OUTPUT_SCHEMA")
    fi
    [ "$MODE" = "vision" ] && command+=(--image "$IMAGE_FILE")
    if [ "$CODEX_SESSION_MODE" = "1" ] && [ -n "${ARTICLE_CODEX_RESUME_SESSION_ID:-}" ]; then
      # Verified against codex-cli 0.145.0 on 2026-08-07: resuming by explicit
      # session id returns the same thread_id with its context carried forward.
      # `resume --last` is deliberately not used: it picks the newest session
      # for this working directory, which other loops share, so it cannot prove
      # it is continuing this investigation rather than a stranger's session.
      command+=(resume "$ARTICLE_CODEX_RESUME_SESSION_ID")
    fi
    provider_config_rc=0
    if [ "$CODEX_PROVIDER_ID" != "openai" ]; then
      if ! configure_codex_provider; then
        printf '%s\n' "model-runner: invalid Codex provider configuration" >"$raw_err"
        provider_config_rc=69
      else
        command+=(
          -c "model_provider=\"$CODEX_PROVIDER_ID\""
          -c "model_providers={$CODEX_PROVIDER_ID={name=\"Writer provider\",base_url=\"$CODEX_PROVIDER_BASE_URL\",env_key=\"$CODEX_PROVIDER_ENV_KEY\",wire_api=\"responses\"}}"
        )
      fi
    fi
    command+=(-)
  elif [ "$MODE" = "agent" ]; then
    command=(
      "$binary" --model sonnet
      --dangerously-skip-permissions
      --add-dir "$HOME"
      --no-session-persistence
      -p "$prompt"
    )
  elif [ "$MODE" = "judge" ]; then
    command=(
      "$binary" --model sonnet
      --no-session-persistence
      --allowedTools ""
      -p "$prompt"
    )
  else
    prompt="${prompt}

Read and judge this existing image file: $IMAGE_FILE"
    command=(
      "$binary" --model sonnet
      --no-session-persistence
      --allowedTools "Read"
      --add-dir "$(dirname "$IMAGE_FILE")"
      -p "$prompt"
    )
  fi

  errexit_was_set=0
  case "$-" in
    *e*) errexit_was_set=1 ;;
  esac
  set +e
  if [ "$provider_config_rc" -ne 0 ]; then
    rc="$provider_config_rc"
  elif [ "$provider" = "codex" ]; then
    if [ "$MODE" = "agent" ]; then
      # Mark every process inside the bounded sandbox so nested judge/vision
      # calls route through the wrapper-side judge broker instead of trying
      # to start a provider process the sandbox will deny.
      ARTICLE_NESTED_SANDBOX=1 "${command[@]}" <"$PROMPT_FILE" >"$raw_out" 2>"$raw_err"
    else
      "${command[@]}" <"$PROMPT_FILE" >"$raw_out" 2>"$raw_err"
    fi
    rc=$?
  else
    "${command[@]}" >"$raw_out" 2>"$raw_err"
    rc=$?
  fi
  if [ "$errexit_was_set" -eq 1 ]; then
    set -e
  else
    set +e
  fi

  if [ "$CODEX_SESSION_MODE" = "1" ] && [ "$provider" = "codex" ]; then
    # The caller owns the event stream file and parses it directly. Echoing a
    # whole JSONL transcript into the shared model log would bloat it without
    # adding a reader, so only a one-line summary is logged here.
    printf '%s run_id=%s provider=%s mode=%s status=session_stream events=%s rc=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RUN_ID" "$provider" "$MODE" \
      "$raw_out" "$rc" >>"$MODEL_LOG"
    : >"$safe_out"
  else
    python3 "$SUPPORT" redact <"$raw_out" >"$safe_out"
  fi
  python3 "$SUPPORT" redact <"$raw_err" >"$safe_err"
  if [ -s "$safe_err" ]; then
    # The provider result is authoritative. A detached caller may close its
    # stderr while the durable run continues; keep the file log and never let
    # tee's SIGPIPE replace a successful provider return code.
    tee -a "$MODEL_LOG" <"$safe_err" >&2 || true
  fi
  if [ -s "$safe_out" ]; then
    tee -a "$MODEL_LOG" <"$safe_out" || true
  fi

  INVOCATION_STDOUT="$raw_out"
  INVOCATION_STDERR="$raw_err"
  INVOCATION_SAFE_STDOUT="$safe_out"
  INVOCATION_SAFE_STDERR="$safe_err"
  INVOCATION_RC="$rc"
}

cleanup_invocation() {
  local path
  for path in \
    "${INVOCATION_STDOUT:-}" "${INVOCATION_STDERR:-}" \
    "${INVOCATION_SAFE_STDOUT:-}" "${INVOCATION_SAFE_STDERR:-}"; do
    # The session-mode event stream belongs to the caller and is the durable
    # evidence a checkpoint is rebuilt from; never delete it here.
    [ -n "$path" ] && [ "$path" = "${ARTICLE_CODEX_EVENTS_FILE:-}" ] && continue
    [ -n "$path" ] && [ -f "$path" ] && rm -f -- "$path"
  done
  INVOCATION_STDOUT=""
  INVOCATION_STDERR=""
  INVOCATION_SAFE_STDOUT=""
  INVOCATION_SAFE_STDERR=""
}

if [ "$PROVIDER" = "auto" ]; then
  CANDIDATES=(codex claude)
else
  CANDIDATES=("$PROVIDER")
fi

for candidate in "${CANDIDATES[@]}"; do
  binary="$(provider_binary "$candidate")"
  if [ -z "$binary" ] || [ ! -x "$binary" ]; then
    log_event "$candidate" "unavailable" "reason=missing_cli"
    if [ "$PROVIDER" != "auto" ]; then
      echo "model-runner: provider=$candidate missing_cli" >&2
      exit 69
    fi
    continue
  fi

  if ! is_eligible "$candidate"; then
    log_event "$candidate" "skipped" "reason=cooldown"
    if [ "$PROVIDER" != "auto" ]; then
      echo "model-runner: provider=$candidate mode=$MODE is in cooldown" >&2
      exit "$TEMP_FAILURE"
    fi
    continue
  fi

  invoke_provider "$candidate"
  rc="$INVOCATION_RC"
  if [ "$rc" -eq 0 ]; then
    record_health "$candidate" success
    log_event "$candidate" success
    cleanup_invocation
    exit 0
  fi

  error_class="$(python3 "$SUPPORT" classify \
    --return-code "$rc" --stdout "$INVOCATION_STDOUT" --stderr "$INVOCATION_STDERR")"
  if [ "$error_class" = "fatal" ]; then
    record_health "$candidate" fatal "$error_class"
    log_event "$candidate" fatal "error_class=$error_class rc=$rc"
    cleanup_invocation
    exit "${rc:-1}"
  fi

  record_health "$candidate" retryable "$error_class"
  log_event "$candidate" retryable "error_class=$error_class rc=$rc"
  cleanup_invocation

  # A full foreground agent may already have created public side effects.
  # Never replay that complete prompt on another provider after it starts.
  if [ "$MODE" = "agent" ]; then
    exit "$TEMP_FAILURE"
  fi
  if [ "$PROVIDER" != "auto" ]; then
    exit "$TEMP_FAILURE"
  fi
done

echo "model-runner: no healthy provider for mode=$MODE" >&2
exit "$TEMP_FAILURE"
