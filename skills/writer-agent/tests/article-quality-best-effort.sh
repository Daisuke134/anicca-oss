#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DAILY="$ROOT/skills/writer-agent/article-daily.sh"
SKILL="$ROOT/skills/writer-agent/SKILL.md"

# Quality exhaustion must no longer terminate the topic as carry-over.
! rg -q 'carry-over:bounded-revise|carry-over:reader-testing' "$DAILY" "$SKILL"

# The policy must preserve evidence while continuing to safety and publication.
rg -q 'record-quality-advisory\.py|telemetry/advisory' "$DAILY" "$SKILL"
rg -q 'best current draft' "$DAILY" "$SKILL"
rg -q 'continue to STEP 4\.7' "$DAILY"
rg -q 'quality.*429|429.*quality' "$DAILY"
rg -q 'one initial evaluation and at most two revise-then-evaluate cycles' "$DAILY"
rg -q 'record-quality-advisory\.py' "$DAILY"
! rg -q '5 initial|fresh 5|20 total STEP 4\.6|revise up to 5' "$DAILY" "$SKILL"

# Safety remains a real blocker for the affected topic and routes to an alternate.
rg -q 'conscience.*BLOCK|BLOCK.*conscience' "$DAILY"
rg -q 'alternate topic|different alternate topic' "$DAILY"

# Delivery and evidence remain isolated and honest.
rg -q 'platform.*independent|independent.*platform' "$DAILY"
rg -q 'reality-gate.*PASS|reality_gate.*PASS' "$DAILY"
rg -q 'published:true' "$DAILY"
rg -q 'article-run-complete\.py' "$DAILY"
rg -q -- '--run-id' "$DAILY"
rg -q 'publication-guard\.py' "$DAILY"

echo 'PASS: daily writer treats quality as advisory, safety as blocking, and platforms independently'
