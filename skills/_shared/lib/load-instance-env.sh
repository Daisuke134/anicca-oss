#!/usr/bin/env bash
# _shared/lib/load-instance-env.sh — SOURCED (never executed directly) by skill run.sh wrappers to
# load shared per-user secrets from the canonical Life Manager environment WITHOUT letting the
# file's own ANICCA_HOME assignment clobber the caller's per-instance ANICCA_HOME.
#
# Root cause (anicca-spawn-identity-resolution-fix, 2026-07-09): $HOME is the real, SHARED macOS home
# — every instance on one machine may source the SAME file — while ANICCA_HOME is per-instance
# identity. The shared environment may set its own ANICCA_HOME;
# sourcing it under `set -a` used to silently overwrite the caller's correct ANICCA_HOME wholesale,
# which broke self/spawn/run.sh's real identity resolution in production. Hand-copying the fix into
# each affected run.sh (self/spawn, self/spawn-child, economy/lending) let a 4th, unfixed instance
# slip through in review — this file is the single source of truth so that never recurs: any run.sh
# that needs these shared secrets sources THIS file instead of re-implementing the preamble.
#
# Usage (from a skill's run.sh):
#   . "$SKILL_DIR/../../_shared/lib/load-instance-env.sh"
# (or any equivalent path expression that resolves to this file — see call sites for examples).
set -a
_ANICCA_HOME_CALLER="${ANICCA_HOME:-}"
_LIFE_MANAGER_ENV_FILE="${LIFE_MANAGER_ENV_FILE:-$HOME/.local/state/life-manager/.env}"
[ -f "$_LIFE_MANAGER_ENV_FILE" ] && . "$_LIFE_MANAGER_ENV_FILE"
[ -n "$_ANICCA_HOME_CALLER" ] && ANICCA_HOME="$_ANICCA_HOME_CALLER"
unset _ANICCA_HOME_CALLER _LIFE_MANAGER_ENV_FILE
set +a
