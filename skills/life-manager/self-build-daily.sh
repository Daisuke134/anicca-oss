#!/usr/bin/env bash
# One bounded Life Manager SELF-BUILD pass (spec §10 row 10f, §9.3 DEV loop).
#
# This is the launchd entrypoint for `ai.anicca.life-manager-selfbuild` — the CONSUMER. It does
# exactly one thing per day: hand the oldest eligible loop-authored fix PR to the 10e merge guard,
# then report what actually happened. It decides nothing about merging — every gate lives in the
# guard. The PRODUCER (`ai.anicca.life-manager-dev`) is a different job with a different label.
#
# It mirrors life-manager-daily.sh: PATH first, recursion guard, env sourcing, one log file, one
# honest Telegram report at the end. Differences are deliberate:
#   * the report is sent whatever the outcome, INCLUDING a no-op day, because 10f's done condition
#     is seven distinct days each carrying an honest outcome — a silent day looks like a dead loop;
#   * the report is built from the LAST LINE OF THE LEDGER, never from the CLI's stdout and never
#     from a claim. Those differ in exactly the cases that matter: a pass that appended its row and
#     then died before printing would otherwise report nothing, and a pass that printed a row it
#     never managed to append would report a day that does not exist. The ledger is the evidence
#     10f is measured by, so the ledger is what gets read.
#
# This script never loads, unloads or edits its own launchd job — a loop that can schedule itself
# can also un-pause itself. Enabling the schedule is a separate, explicit operator step:
#   apps/life-manager/scripts/enable-self-build-launchd.sh
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:$PATH"

# The day boundary is Asia/Tokyo, and "seven distinct days" is the entire done condition. launchd's
# StartCalendarInterval carries no time-zone field, so the SCHEDULE follows whatever the machine is
# set to; re-asserting TZ here means that even if the machine's zone is changed months from now, or
# the plist is edited, or this script is run by hand from some other context, the pass's own day
# arithmetic cannot drift and start splitting or merging days.
export TZ=Asia/Tokyo

set -uo pipefail
umask 077

if [ "${LM_SELFBUILD_ACTIVE:-0}" = "1" ]; then
  printf 'self-build-daily: recursive invocation blocked\n' >&2
  exit 73
fi
export LM_SELFBUILD_ACTIVE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${LM_SELFBUILD_REPO:-$(cd "$HERE/../.." && pwd)}"
APP_DIR="$REPO_ROOT/apps/life-manager"
DISK_GUARD="$REPO_ROOT/skills/earn/gig/scripts/gig_disk_guard.py"
NODE_BIN="${NODE_BIN:-$(command -v node || echo /opt/homebrew/bin/node)}"
DAILY_CLI="$APP_DIR/scripts/self-build-daily.js"
LIFE_MANAGER_STATE_HOME="${LIFE_MANAGER_STATE_HOME:-$HOME/.local/state/life-manager}"
ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$LIFE_MANAGER_STATE_HOME/.env}"
LOG="${LM_SELFBUILD_LOG:-$LIFE_MANAGER_STATE_HOME/logs/life-manager-self-build.log}"
LEDGER="${LM_SELFBUILD_LEDGER:-$LIFE_MANAGER_STATE_HOME/state/self-build-days.jsonl}"
readonly LM_SELFBUILD_CANONICAL_DISK_GUARD="$DISK_GUARD"
readonly LM_SELFBUILD_CANONICAL_HOST_STATE="$HOME/.openclaw/state"
readonly LM_SELFBUILD_CANONICAL_STATE_HOME="$LIFE_MANAGER_STATE_HOME"
mkdir -p "$(dirname "$LOG")"
printf '=== life-manager self-build run %s (TZ=%s) ===\n' "$(date '+%F %T %Z')" "$TZ" >>"$LOG"

# Credentials for gh / railway / the reviewer CLI live here. `set +u` because a launchd environment
# is not an interactive one and a dotenv file is allowed to reference unset variables.
if [ -f "$ENV_FILE" ]; then
  set +u
  set -a
  # shellcheck disable=SC1091
  . "$ENV_FILE"
  set +a
  set -u
fi

# Restore only the guard's canonical paths from before dotenv loading. Other
# runtime overrides (including explicit LM_SELFBUILD_LOG/LEDGER) retain their
# existing semantics.
DISK_GUARD="$LM_SELFBUILD_CANONICAL_DISK_GUARD"

# Self-build is a write-heavy producer. Pin the repository's canonical floor and
# host/state roots after dotenv loading so runtime configuration cannot lower or
# redirect the shared guard boundary.
GIG_DISK_HEADROOM_KIB=524288
GIG_HOST_STATE_DIR="$LM_SELFBUILD_CANONICAL_HOST_STATE"
GIG_STATE_DIR="$LM_SELFBUILD_CANONICAL_STATE_HOME"
export GIG_DISK_HEADROOM_KIB
export GIG_HOST_STATE_DIR GIG_STATE_DIR

# The dotenv file is allowed to set runtime values, but it can never bypass the
# shared producer stop contract.
unset GIG_IGNORE_DISK_PRESSURE_BLOCK GIG_IGNORE_DISK_WRITERS_STOP
unset DISK_CONTROL_STATE_DIR OPENCLAW_STATE_DIR LIFE_MANAGER_HOST_STATE_DIR

TG_TARGET="${LM_SELFBUILD_TELEGRAM_TARGET:?LM_SELFBUILD_TELEGRAM_TARGET is required}"

if ! /usr/bin/python3 "$DISK_GUARD" /usr/bin/true >>"$LOG" 2>&1; then
  printf 'self-build: disk guard blocked before dependency install\n' >>"$LOG"
  exit 1
fi

if [ ! -d "$APP_DIR/node_modules/pg" ]; then
  (cd "$APP_DIR" && npm ci --silent) >>"$LOG" 2>&1 || printf 'dependency install failed\n' >>"$LOG"
fi

# LM_SELFBUILD_DRY_RUN=1 exercises this whole path — env sourcing, the pass, the report render, the
# Telegram send — with downstream effects stubbed out, so the entrypoint can be verified without promoting
# anything. launchd never sets it. This is not a merge switch: the guard, not this flag, decides
# every gate, and skills/** is on the guard's DENY list so no unattended PR can touch this file.
DAILY_ARGS=()
[ "${LM_SELFBUILD_DRY_RUN:-0}" = "1" ] && DAILY_ARGS+=(--dry-run)

if ! /usr/bin/python3 "$DISK_GUARD" /usr/bin/true >>"$LOG" 2>&1; then
  printf 'self-build: disk guard blocked before daily CLI\n' >>"$LOG"
  exit 1
fi

RESULT="$("$NODE_BIN" "$DAILY_CLI" "${DAILY_ARGS[@]+"${DAILY_ARGS[@]}"}" 2>>"$LOG")"
RC=$?
printf '%s\n' "$RESULT" >>"$LOG"

# THE REPORT READS THE LEDGER, NOT THE STDOUT ABOVE. $RESULT is kept only to tell the reader which
# exit code went with the day. If the last ledger line is missing, unparseable, or older than this
# run, that IS the report — loudly — because a pass that cannot point at its own appended row has
# not proven a day happened, whatever it printed.
# shellcheck disable=SC2016  # the ${...} below are JS template literals, deliberately unexpanded
REPORT="$(LEDGER="$LEDGER" LOG_PATH="$LOG" RC="$RC" "$NODE_BIN" -e '
const fs = require("node:fs");
const ledger = String(process.env.LEDGER || "");
let line = "";
try {
  line = fs.readFileSync(ledger, "utf8").split("\n").filter((value) => value.trim()).pop() || "";
} catch {
  line = "";
}
let row = null;
try { row = JSON.parse(line); } catch {}
if (!row || typeof row !== "object" || !row.day) {
  process.stdout.write(
    `⚠️ Life Manager self-build: NO LEDGER ROW. The daily pass exited ${process.env.RC} but the last`
    + ` line of ${ledger} is not a readable day row, so no day was proven and nothing can be`
    + ` reported about what the guard did. Check ${process.env.LOG_PATH || "the configured log"} and`
    + ` the dev-guard ledger by hand.`,
  );
} else {
  const target = row.pr ? `PR #${row.pr}` : "no PR";
  const why = row.no_op_reason ? ` (${row.no_op_reason})` : "";
  const raw = row.no_op_reason_raw ? ` [raw: ${row.no_op_reason_raw}]` : "";
  const guard = row.guard_run_ref ? `\nguard run: ${row.guard_run_ref}` : "";
  const err = row.error ? `\nerror: ${row.error}` : "";
  const stale = row.recovered_stale_lock ? "\nrecovered a stale guard lock (>30min)" : "";
  const over = row.budget_exceeded
    ? `\nover budget${row.merge_in_flight ? " — waited, because the merge had already begun" : ""}`
    : "";
  const cleanup = row.cleanup_ran ? "\nran git worktree prune after the kill" : "";
  // A killed PRE-merge run cannot have merged. A timeout row still gets the loudest line in this
  // report, because the ONE thing this pass cannot rule out by itself is a merge it did not see.
  const danger = row.verdict === "timeout"
    ? "\n⚠️ main may be merged & unverified — check dev-guard ledger now"
    : "";
  const skipped = Array.isArray(row.skipped) && row.skipped.length
    ? `\nskipped: ${row.skipped.map((s) => `#${s.pr} [${s.reasons.join(",")}]`).join(" ")}`
    : "";
  process.stdout.write(
    `🤖 Life Manager self-build ${row.day}\n`
    + `${target} -> ${row.verdict}${why}${raw}${guard}${stale}${over}${cleanup}${err}${danger}${skipped}`,
  );
}
')"

# The seven-day readout comes from the CLI, which reads the same ledger — kept separate so a broken
# streak calculation can never stop the day itself from being reported.
# shellcheck disable=SC2016  # the ${...} below are JS template literals, deliberately unexpanded
STREAK="$("$NODE_BIN" "$DAILY_CLI" --status 2>>"$LOG" | "$NODE_BIN" -e '
const raw = require("node:fs").readFileSync(0, "utf8").trim().split("\n").pop() || "";
let value = null;
try { value = JSON.parse(raw); } catch {}
const streak = value && value.streak ? value.streak : null;
process.stdout.write(streak
  ? `day ${streak.distinctDays || 0}/${streak.required || 7}`
    + (streak.ready ? " — seven-day condition MET" : ` (${streak.remaining ?? "?"} to go)`)
  : "day ?/7 — the streak could not be read");
')"

REPORT="$REPORT
$STREAK"

printf '%s\n' "$REPORT" >>"$LOG"
openclaw message send --channel telegram --target "$TG_TARGET" \
  --message "$REPORT" --json >>"$LOG" 2>&1 || printf 'Telegram report failed\n' >>"$LOG"

printf '=== life-manager self-build done rc=%s %s ===\n' "$RC" "$(date '+%F %T %Z')" >>"$LOG"
if [ "$RC" -eq 0 ]; then
  mkdir -p "$LIFE_MANAGER_STATE_HOME/state"
  touch "$LIFE_MANAGER_STATE_HOME/state/.life-manager-self-build-last-pass"
fi
exit "$RC"
