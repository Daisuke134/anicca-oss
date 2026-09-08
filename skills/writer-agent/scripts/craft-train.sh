#!/usr/bin/env bash
# craft-train.sh — nightly SkillOpt trainer for skills/writing-craft/CRAFT.md
# (spec 47 19-4c T6, BUILD 3).
#
# Every night, SkillOpt proposes at most two bounded edits to CRAFT.md (the
# vendor adapter's edit_budget=2), and the edits survive only if held-out
# beat rate strictly improves. All decision logic (guard, accept/reject,
# sha256 compare, protected-block safety) lives in craft_train.py so it can
# be driven by fixtures in the contract test with no live model call and no
# live network -- this script's own job is just: export the two
# AZURE_OPENAI_* env vars SkillOpt's openai_chat/OpenAI-compatible mode needs
# (NEVER echo the key -- without these, SkillOpt silently addresses
# api.openai.com with the key "dummy" and burns every retry on a 401, and a
# launchd job inherits no environment, so this is the difference between
# learning and a quiet no-op every night), then delegate.
#
# Exits 0 whether or not an edit was accepted, and exits 0 on the
# fewer-than-3-scored-runs guard too -- a night that learns nothing is a
# normal night; a nonzero exit would make launchd look broken.
#
# T15 wall-clock follow-up: this script also owns the hard deadline (part
# 3) -- craft_train.py's own projection check (part 4) refuses to START a
# run that would not finish before it, and its subprocess timeout stops
# SkillOpt cleanly at it if a run somehow runs long anyway, but BOTH of
# those live inside the python process this script launches. If that
# process itself hangs outside its own subprocess call (a bug, not the
# expected path), this script's own `timeout` wrapper below is the
# last-resort kill switch that guarantees "regardless of progress" is
# actually true, not just "true unless craft_train.py itself breaks".
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="${ARTICLE_SKILL_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CRAFT_MD="${CRAFT_MD:-$SKILL_DIR/reference/CRAFT.md}"
VENDOR_DIR="$SKILL_DIR/vendor/skillopt-writing"
CLIPROXY_CONF="${CLIPROXY_CONF:-${ARTICLE_CLIPROXY_CONFIG:-}}"
CLIPROXY_PORT="${CLIPROXY_PORT:-8317}"
OPENAI_BASE_URL="${ARTICLE_OPENAI_BASE_URL:-http://127.0.0.1:${CLIPROXY_PORT}/v1}"

PY="${ARTICLE_PYTHON:-${WRITER_BROWSER_PYTHON:-${LIFE_MANAGER_PYTHON:-$(command -v python3)}}}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
SKILLOPT_PYTHON="${SKILLOPT_PYTHON:-$PY}"

STATE_DIR="${ARTICLE_STATE_DIR:-$SKILL_DIR/state}"
JSONL="${CRAFT_TRAIN_JSONL:-$STATE_DIR/craft-train.jsonl}"
RUNS_ROOT="${CRAFT_TRAIN_RUNS_ROOT:-$STATE_DIR/runs}"
CONFIG="$VENDOR_DIR/configs/writing/default.yaml"
RUN_TRAIN="$VENDOR_DIR/run_train.py"
SPLIT_DIR="$VENDOR_DIR/data/writing_split"
OUT_ROOT="$STATE_DIR/craft-train-output/$(date -u +%Y%m%dT%H%M%SZ)"

# The hard deadline: the next LOCAL occurrence of DEADLINE_HOUR:DEADLINE_MINUTE
# (05:00 by default, ahead of the 06:00 local publish). If that time today
# has already passed, it rolls to tomorrow -- the normal case for a job
# that starts at 23:10.
DEADLINE_HOUR="${CRAFT_TRAIN_DEADLINE_HOUR:-05}"
DEADLINE_MINUTE="${CRAFT_TRAIN_DEADLINE_MINUTE:-00}"
DEADLINE_VALUES="$("$PY" "$SCRIPT_DIR/craft_deadline.py" "$DEADLINE_HOUR" "$DEADLINE_MINUTE" 2>/dev/null || true)"
read -r NOW_EPOCH DEADLINE_EPOCH <<<"$DEADLINE_VALUES"
if [ -z "${NOW_EPOCH:-}" ] || [ -z "${DEADLINE_EPOCH:-}" ]; then
  echo "craft-train.sh: could not compute today's ${DEADLINE_HOUR}:${DEADLINE_MINUTE} deadline -- refusing to run without a deadline" >&2
  exit 0
fi

if [ ! -f "$CRAFT_MD" ]; then
  echo "craft-train.sh: CRAFT_MD not found at $CRAFT_MD" >&2
  exit 0
fi

# A caller may invoke this wrapper through `bash -x` or inherit xtrace.
# Disable it before any credential value is read or exported.
set +x

# Read the local CLIProxyAPI key at runtime -- NEVER echo it. Extract only
# the first entry under `api-keys:` and export it directly into the child
# process's environment; it is never printed, logged, or written to a file.
RAW_KEY="${ARTICLE_OPENAI_API_KEY:-}"
if [ -z "$RAW_KEY" ] && [ -n "$CLIPROXY_CONF" ] && [ -f "$CLIPROXY_CONF" ]; then
  RAW_KEY="$(awk '/^api-keys:/{f=1; next} f && /^[[:space:]]*-/{print; exit}' "$CLIPROXY_CONF" \
    | sed -E 's/^[[:space:]]*-[[:space:]]*"?([^"]*)"?[[:space:]]*$/\1/')"
fi

if [ -z "$RAW_KEY" ]; then
  echo "craft-train.sh: ARTICLE_OPENAI_API_KEY or a configured CLIProxy key file is required" >&2
  exit 0
fi

export AZURE_OPENAI_ENDPOINT="$OPENAI_BASE_URL"
export AZURE_OPENAI_API_KEY="$RAW_KEY"
export AZURE_OPENAI_AUTH_MODE="openai_compatible"
unset RAW_KEY

if [ ! -x "$SKILLOPT_PYTHON" ]; then
  echo "craft-train.sh: skillopt python not found/executable at $SKILLOPT_PYTHON" >&2
  exit 0
fi
if ! "$SKILLOPT_PYTHON" -c 'from skillopt.model.backend_config import set_optimizer_backend, set_target_backend; set_optimizer_backend("openai_chat"); set_target_backend("openai_chat"); import scripts.train' >/dev/null 2>&1; then
  echo "craft-train.sh: managed runtime is missing skillopt==0.2.0" >&2
  exit 0
fi

# Outer, last-resort kill switch (see the module comment above): generous
# padding past the deadline so craft_train.py's own clean internal
# handling (project-and-refuse, then the subprocess timeout) is what fires
# under every normal path -- this only matters if craft_train.py itself
# hangs somewhere outside its own subprocess call.
OUTER_TIMEOUT_S=$(( DEADLINE_EPOCH - NOW_EPOCH + 300 ))
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"

# Not `exec`: craft_train.py already returns 0 on every path (guard,
# infra failure, reject, accept, even an unhandled exception), but this is
# belt-and-suspenders -- a night that learns nothing must never look like
# a broken launchd job, so this wrapper's own exit is pinned to 0 no
# matter what the child process (or the outer timeout wrapper) does.
if [ -n "$TIMEOUT_BIN" ]; then
  "$TIMEOUT_BIN" "${OUTER_TIMEOUT_S}s" "$PY" "$SKILL_DIR/scripts/craft_train.py" \
    --craft-md "$CRAFT_MD" \
    --config "$CONFIG" \
    --split-dir "$SPLIT_DIR" \
    --run-train "$RUN_TRAIN" \
    --skillopt-python "$SKILLOPT_PYTHON" \
    --runs-root "$RUNS_ROOT" \
    --out-root "$OUT_ROOT" \
    --jsonl "$JSONL" \
    --deadline-epoch "$DEADLINE_EPOCH"
else
  echo "craft-train.sh: no timeout/gtimeout binary found -- running without the outer kill switch, relying on craft_train.py's own internal deadline" >&2
  "$PY" "$SKILL_DIR/scripts/craft_train.py" \
    --craft-md "$CRAFT_MD" \
    --config "$CONFIG" \
    --split-dir "$SPLIT_DIR" \
    --run-train "$RUN_TRAIN" \
    --skillopt-python "$SKILLOPT_PYTHON" \
    --runs-root "$RUNS_ROOT" \
    --out-root "$OUT_ROOT" \
    --jsonl "$JSONL" \
    --deadline-epoch "$DEADLINE_EPOCH"
fi
exit 0
