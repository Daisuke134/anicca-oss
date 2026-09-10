#!/usr/bin/env bash
# _instance_paths.sh — resolve per-ANICCA_INSTANCE paths (queue/posted/accounts/ledger).
# Sourced by run.sh / producer.sh / monitor.sh. Unset ANICCA_INSTANCE => identical to the
# pre-split paths (zero regression for the already-live claude-p/myclaude loop).
#
# Two independent identities (e.g. claude-p human-funded vs a ClawRouter self-funded
# instance) run the SAME scripts against DIFFERENT wallets/accounts/ledgers by only
# differing in this one env var — never share a queue/account/ledger across instances
# (shared state across earners is the drain/duplicate-post pitfall this file exists to avoid).
ANICCA_INSTANCE="${ANICCA_INSTANCE:-}"
_SFX=""
[ -n "$ANICCA_INSTANCE" ] && _SFX="-${ANICCA_INSTANCE}"

CLIP_QUEUE="${HOME}/clips/queue${_SFX}"
CLIP_POSTED="${HOME}/clips/posted${_SFX}"
CLIP_ACCTS="${HOME}/.cloak/clip-accounts${_SFX}.json"
CLIP_STATE_ROOT="${LIFE_MANAGER_STATE_ROOT:-${HOME}/.local/state/life-manager/clip-loop}"
CLIP_LEDGER="${EARN_LEDGER:-${CLIP_STATE_ROOT}/clip-earn-ledger${_SFX}.jsonl}"
# REQ-006 (clip-post-verify-hardening): clips with an unverified post outcome move here
# (never back to CLIP_QUEUE — duplicate-post risk; never to CLIP_POSTED — false-confirmation
# risk) so a later wake's self-heal driver can reconcile them via post_reel.py --verify-only.
CLIP_PENDING_VERIFY="${HOME}/clips/pending-verify${_SFX}"
