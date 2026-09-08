#!/usr/bin/env bash
# gen-plus-address.sh — print a fresh unique Gmail plus-address for a self-signup attempt.
# Generalized from ig-account-create's proven pattern (spec §7.55, docs/superpowers/specs/
# 2026-07-14-article-earn-loop-ssot.md): Gmail plus-addressing delivers
# every +tag variant to the SAME inbox, giving infinite unique-looking addresses without
# needing a disposable-email provider (which platforms auto-flag/suspend — proven failure
# mode: agentmail.to killed @aiclipper.daily on Instagram). Read the code/magic-link back
# with `gog gmail search --account <base> "<query>" --max 3 --plain` (needs
# GOG_KEYRING_PASSWORD in env, loaded through LIFE_MANAGER_ENV_FILE).
#
# Usage: bash gen-plus-address.sh <platform-tag>
#   e.g. SELF_SIGNUP_GMAIL_BASE=owner bash gen-plus-address.sh substack
set -uo pipefail

GMAIL_BASE="${SELF_SIGNUP_GMAIL_BASE:?SELF_SIGNUP_GMAIL_BASE is required}"
PLATFORM="${1:?usage: gen-plus-address.sh <platform-tag>}"
RAND="$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
echo "${GMAIL_BASE}+${PLATFORM}-${RAND}@gmail.com"
