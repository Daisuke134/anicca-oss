#!/usr/bin/env bash
# earn/clip-producer — ON-DEMAND wrapper around the repository-owned ../clip/producer.sh.
# It is a bounded media-generation capability; the retired standalone launchd/tmux owners
# and the removed posting slot are not required.
#
# Why this is safe to slot-ify (unlike ig-account-create): producer.sh is a fully
# DETERMINISTIC pipeline (sliced yt-dlp DL -> whisper -> highlight pick -> 9:16 crop ->
# caption burn -> verify_clip gate) with NO visual/CDP judgment step -- no screenshots, no
# clickxy, nothing that requires an agentic vision loop. A single bash entrypoint is
# sufficient; genuinely different from ig-account-create (spec 2026-07-04-openclaw-claude-p
# -merge-design.md §8).
#
set -uo pipefail
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/../clip" && pwd)/producer.sh" "$@"
