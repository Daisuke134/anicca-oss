#!/bin/bash
# run_decision_loop.sh — launchd entrypoint for decision_loop.py (2026-07-25 decision-loop task).
# THIS FILE DECIDES NOTHING (same convention as run.sh). decision_loop.py is stdlib-only at its
# own top level (json/os/re/subprocess/datetime + pinnacle_edge/pinnacle_observe). Every child uses
# the same bootstrap-managed Life Manager Python and installation environment.
#
# DRY BY DEFAULT: decision_loop.py itself forces PM_DRY_RUN=1 on every child unless BOTH
# PM_DRY_RUN=0 AND PM_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK are already set in ITS OWN environment —
# this launchd job sets neither, so it can never go live no matter what changes elsewhere on the
# machine (see decision_loop.py's _live_confirmed() docstring).
set -u
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SKILL_DIR"
REPO_ROOT="$(cd "$SKILL_DIR/../../.." && pwd -P)"
# shellcheck disable=SC1091
source "$REPO_ROOT/runtime/earn/polymarket-runtime-env.sh" || exit $?
exec "$LIFE_MANAGER_PYTHON" decision_loop.py
