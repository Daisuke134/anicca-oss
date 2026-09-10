#!/usr/bin/env bash
# Enable the daily DEV SELF-BUILD (consumer) schedule (spec §10 row 10f).
#
# THIS SCRIPT IS SHIPPED, NOT RUN, BY THE ATOMIC THAT ADDED IT. Loading a launchd job is a real,
# recurring side effect on Dais's machine; it happens once, deliberately, after the PR is merged.
# Nothing in the loop calls this file — the loop must never be able to schedule itself.
#
# TWO JOBS, TWO LABELS. Getting this wrong is how the loop eats itself:
#
#   ai.anicca.life-manager-dev        PRODUCER. Runs scripts/life-manager-dev-d0.sh: reads a
#                                     self-heal issue, writes a fix, opens a PR. It never merges.
#                                     Currently PARKED (…​.plist.disabled). THIS SCRIPT NEVER
#                                     TOUCHES IT — not the plist, not the label, not launchctl.
#
#   ai.anicca.life-manager-selfbuild  CONSUMER. Runs skills/life-manager/self-build-daily.sh: takes
#                                     the oldest eligible producer PR and hands it to the merge
#                                     guard. This is the job this script installs.
#
# An earlier version of this file wrote the CONSUMER into the PRODUCER's label. Enabling the
# consumer would have silently unscheduled the producer — the loop would have drained the existing
# backlog, found nothing new ever again, and reported seven honest no-op days as success. A consumer
# with no producer is a loop with no input; they are two jobs and they get two labels.
#
# The producer is NOT revived here. Whether it should run again is a decision with its own blast
# radius (it opens PRs and sends Telegram), so this script prints the exact command and stops.
#
# What it does, in order:
#   1. refuses unless the repo root is a real main checkout — on main, not a worktree, clean tree;
#   2. refuses unless the three files the job needs exist AT HEAD OF MAIN (git show, not the
#      working tree — the working tree can hold anything, including this branch);
#   3. refuses unless every binary the job needs resolves under the job's OWN environment
#      (`env -i PATH=…`), because a login shell's PATH is not launchd's;
#   4. writes the consumer plist under its own label, pinned to 04:10 with TZ=Asia/Tokyo;
#   5. bootstraps it and reads back what launchd actually thinks, rather than assuming.
#
# TIME ZONE. launchd StartCalendarInterval is LOCAL time with no time-zone field (Apple, "Creating
# Launch Daemons and Agents"). This machine runs Asia/Tokyo, so Hour=4 Minute=10 IS 04:10 JST. The
# script asserts the machine's zone rather than trusting it — and because a machine's zone can be
# changed six months from now, TZ is ALSO set in the plist and re-asserted by the entrypoint at run
# time, so the pass's own day math cannot drift even if the schedule does.
#
# Usage:  apps/life-manager/scripts/enable-self-build-launchd.sh          # enable + read back
#         apps/life-manager/scripts/enable-self-build-launchd.sh --status # read back only
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

LABEL="ai.anicca.life-manager-selfbuild"
PRODUCER_LABEL="ai.anicca.life-manager-dev"
AGENTS="$HOME/Library/LaunchAgents"
ACTIVE="$AGENTS/$LABEL.plist"
PRODUCER_PARKED="$AGENTS/$PRODUCER_LABEL.plist.disabled"
PAUSE_MARKER="$HOME/.local/state/life-manager/state/life-manager-dev/PAUSED_UNTIL_FINAL_PHASE"
DOMAIN="gui/$(id -u)"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${LM_SELFBUILD_REPO:-$(cd "$HERE/../../.." && pwd)}"
ENTRYPOINT="$REPO_ROOT/skills/life-manager/self-build-daily.sh"
NODE_BIN="${NODE_BIN:-$(command -v node || echo /opt/homebrew/bin/node)}"
JOB_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

status() {
  printf -- '--- consumer (%s) ---\n' "$LABEL"
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
    | grep -E 'state|last exit|runs =|program|Hour|Minute' || printf 'label not loaded\n'
  printf -- '--- producer (%s) ---\n' "$PRODUCER_LABEL"
  launchctl print "$DOMAIN/$PRODUCER_LABEL" 2>/dev/null \
    | grep -E 'state|last exit|runs =' || printf 'label not loaded (parked; see notes below)\n'
  printf -- '--- ledger ---\n'
  "$NODE_BIN" "$REPO_ROOT/apps/life-manager/scripts/self-build-daily.js" --status || true
}

if [ "${1:-}" = "--status" ]; then
  status
  exit 0
fi

# ------------------------------------------------------------------------------------------------
# 1. The repo root must be the real main checkout.
#
# A plist generated from a worktree pins launchd at a directory that disappears the moment the
# branch merges and the worktree is removed: the job then runs daily against a path that does not
# exist, logs an error nobody reads, and the ledger silently stops growing. Off-main or dirty is the
# same class of problem one step earlier — the code the job will run every night is whatever
# happened to be checked out at the minute somebody typed this command.
# ------------------------------------------------------------------------------------------------
case "$REPO_ROOT" in
  */.worktrees/*)
    printf 'repo root %s is inside .worktrees/ — a worktree is deleted after merge and the\n' "$REPO_ROOT" >&2
    printf 'job would be pinned at a path that no longer exists. Run this from the main checkout.\n' >&2
    exit 1
    ;;
esac
if [ ! -d "$REPO_ROOT/.git" ]; then
  printf 'repo root %s is not a primary git checkout (.git is not a directory)\n' "$REPO_ROOT" >&2
  exit 1
fi
CURRENT_BRANCH="$(git -C "$REPO_ROOT" branch --show-current)"
if [ "$CURRENT_BRANCH" != "main" ]; then
  printf 'repo root is on branch %s, not main; refusing to schedule\n' "$CURRENT_BRANCH" >&2
  exit 1
fi
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
  printf 'repo root has uncommitted changes; refusing to schedule an unattended job over them\n' >&2
  exit 1
fi

# ------------------------------------------------------------------------------------------------
# 2. The three files must exist AT HEAD OF MAIN.
#
# `[ -f path ]` reads the WORKING TREE, which can hold an unstaged file, a leftover from an
# abandoned branch, or something this very PR added and never merged. What the job will run at 04:10
# tomorrow is what is committed on main, so that is what gets checked.
# ------------------------------------------------------------------------------------------------
for required in \
  "skills/life-manager/self-build-daily.sh" \
  "apps/life-manager/lib/dev-merge-guard.js" \
  "apps/life-manager/lib/self-build-daily.js" \
  "apps/life-manager/scripts/self-build-daily.js" \
  "apps/life-manager/scripts/dev-adversary-review.js"
do
  if ! git -C "$REPO_ROOT" show "main:$required" >/dev/null 2>&1; then
    printf '%s is not present at HEAD of main; merge it before enabling the schedule\n' "$required" >&2
    exit 1
  fi
done
[ -x "$ENTRYPOINT" ] || { printf 'entrypoint missing or not executable: %s\n' "$ENTRYPOINT" >&2; exit 1; }

# ------------------------------------------------------------------------------------------------
# 3. Every binary the job needs must resolve under the JOB's environment, not the operator's.
#
# `PATH=$JOB_PATH command -v x` still inherits the whole login environment, so anything a shell
# profile exports can make the probe succeed where launchd will fail. `env -i` is the only honest
# question: nothing but the variables named on the line.
#
# A reviewer that cannot spawn fails CLOSED, so nothing unsafe merges — but the loop would then run
# seven days and merge nothing, and nobody would know why until day seven. The same is true of gh
# (no PR list) and railway (no deploy check). Telegram uses the repository-owned client. Refuse now, naming what is
# missing, rather than discovering it a week later.
# ------------------------------------------------------------------------------------------------
#
# `/bin/sh` by absolute path, not `command` by name: with `env -i` the lookup of the probe program
# itself happens under the scrubbed PATH, so naming a program that has to be FOUND would conflate
# "the binary is missing" with "the probe is missing".
MISSING=""
for binary in node gh railway; do
  # shellcheck disable=SC2016  # "$1" is the INNER sh's positional; expanding it here defeats the point
  env -i PATH="$JOB_PATH" HOME="$HOME" /bin/sh -c 'command -v "$1" >/dev/null 2>&1' sh "$binary" \
    || MISSING="$MISSING $binary"
done
if [ -n "$MISSING" ]; then
  printf 'these binaries do not resolve under the job PATH (%s):%s\n' "$JOB_PATH" "$MISSING" >&2
  printf 'refusing to enable a loop whose tools are missing\n' >&2
  exit 1
fi

REVIEW_CLI_RESOLVED="$(env -i PATH="$JOB_PATH" HOME="$HOME" "$NODE_BIN" -e '
    const { resolveReviewCli } = require(process.argv[1]);
    process.stdout.write(resolveReviewCli(process.env));
  ' "$HERE/dev-adversary-review.js")"
if [ "$REVIEW_CLI_RESOLVED" = "claude" ]; then
  printf 'the fresh-reviewer CLI does not resolve under the job PATH; refusing to enable\n' >&2
  exit 1
fi
printf 'reviewer resolves to: %s\n' "$REVIEW_CLI_RESOLVED"

ZONE="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"
if [ "$ZONE" != "Asia/Tokyo" ]; then
  printf 'machine time zone is %s, not Asia/Tokyo — 04:10 local would not be 04:10 JST\n' "$ZONE" >&2
  exit 1
fi

# ------------------------------------------------------------------------------------------------
# 4. Write the consumer plist. The backup is of $ACTIVE — the file about to be CLOBBERED. Backing up
# anything else is a keepsake, not a rollback.
# ------------------------------------------------------------------------------------------------
[ -f "$ACTIVE" ] && cp "$ACTIVE" "$ACTIVE.bak.$(date +%s)"
cat > "$ACTIVE" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$ENTRYPOINT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>$HOME</string>
    <!-- \$HOME/.local/bin is NOT decoration: measured 2026-07-27, the reviewer CLI (\`claude\`) lives
         there and nowhere else on this machine. The parked plist omitted it, which would have made
         every review fail closed and the loop merge nothing for seven days. -->
    <key>PATH</key>
    <string>$JOB_PATH</string>
    <!-- StartCalendarInterval has no time-zone field, so the SCHEDULE follows the machine. TZ here
         pins the PASS's day arithmetic regardless, and the entrypoint re-exports it at run time. -->
    <key>TZ</key>
    <string>Asia/Tokyo</string>
    <key>LM_SELFBUILD_REPO</key>
    <string>$REPO_ROOT</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>4</integer>
    <key>Minute</key>
    <integer>10</integer>
  </dict>
  <key>RunAtLoad</key>
  <false/>
  <key>StandardOutPath</key>
  <string>$HOME/.local/state/life-manager/logs/life-manager-self-build.out.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/.local/state/life-manager/logs/life-manager-self-build.err.log</string>
</dict>
</plist>
PLIST
chmod 600 "$ACTIVE"
plutil -lint "$ACTIVE"

rm -f "$PAUSE_MARKER"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$ACTIVE"
launchctl enable "$DOMAIN/$LABEL"

status
printf '\nenabled: %s — 04:10 Asia/Tokyo daily (CONSUMER: hands PRs to the merge guard).\n' "$LABEL"
printf 'Merely loading the plist is NOT evidence: the day is proven when a new row appears in the\n'
printf 'self-build ledger.\n'
printf '\nTHE PRODUCER (%s) WAS NOT TOUCHED.\n' "$PRODUCER_LABEL"
printf 'It produces the PRs this consumer eats; without it the backlog drains and never refills.\n'
if [ -f "$PRODUCER_PARKED" ]; then
  printf 'It is still parked at %s.\n' "$PRODUCER_PARKED"
  printf 'Reviving it is a SEPARATE MANUAL step, deliberately not automated here (it opens PRs and\n'
  printf 'sends Telegram). If that is what you want, run BY HAND:\n'
  printf '  cp "%s" "%s/%s.plist"\n' "$PRODUCER_PARKED" "$AGENTS" "$PRODUCER_LABEL"
  printf '  launchctl bootstrap %s "%s/%s.plist"\n' "$DOMAIN" "$AGENTS" "$PRODUCER_LABEL"
  printf '  launchctl enable %s/%s\n' "$DOMAIN" "$PRODUCER_LABEL"
  printf 'Check its PATH and schedule first — it was parked for a reason.\n'
else
  printf 'No parked producer plist found at %s; check why before trusting the backlog.\n' "$PRODUCER_PARKED"
fi
