#!/usr/bin/env bash
# test_startup_prompts_edd.sh — REQ-LV-020/031/113 Tier2 verification: "grep で該当文言が
# STARTUP/cron prompt に含まれることを確認". Checks the STARTUP prompt text itself (not behavior)
# for clip-cli.sh, mirroring this codebase's own grep-based prompt-content test
# convention (verification-architecture.md's stated method for these REQs).
set -uo pipefail
H="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
P=0; F=0
ok(){ echo "  ok $1"; P=$((P+1)); }
fail(){ echo "  FAIL $1"; F=$((F+1)); }

check_contains(){
  local file="$1" needle="$2" name="$3"
  grep -qF "$needle" "$file" && ok "$name" || fail "$name (missing in $file)"
}

CLIP="$H/earn/clip/clip-cli.sh"

echo "--- clip-cli.sh ---"
check_contains "$CLIP" "REQ-LV-020/113" "clip: read-ledgers-before-deciding instruction present"
check_contains "$CLIP" "clip-lessons.jsonl" "clip: lessons ledger path referenced"
check_contains "$CLIP" "REQ-LV-031" "clip: lessons-append instruction present"
check_contains "$CLIP" "beats_previous_week" "clip: weekly-compare evaluator reference present"

echo "=== test_startup_prompts_edd: $P passed $F failed ==="
[ "$F" = 0 ] && exit 0 || exit 1
