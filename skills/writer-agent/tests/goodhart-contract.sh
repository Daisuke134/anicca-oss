#!/usr/bin/env bash
# Contract for the Goodhart-Law feature detector (spec 47 section 19-4a).
#
# NO LIVE NETWORK, NO MODEL CALLS: every check imports the module and drives
# compute_feature_stats with synthetic {title, role, beat_rate} fixtures.
set -uo pipefail

SCRIPT_DIR="${ARTICLE_SKILL_DIR:-$HOME/profitable-claude/skills/writer-agent}/scripts"
PY=/opt/homebrew/bin/python3
command -v "$PY" >/dev/null 2>&1 || PY=python3
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
PASS=0
FAIL=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
    printf 'ok   %s\n' "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %s (expected %s got %s)\n' "$name" "$expected" "$actual"
  fi
}

pyrun() {
  PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}" "$PY" -c "$1" 2>"$TMP/stderr"
}

# 1. A feature that is chosen often but has zero/negative win lift gets
#    flagged -- the proxy-optimisation signature (chosen a lot, does not
#    actually win against real opponents).
OUT1="$(pyrun '
import goodhart as gh

candidates = []
# is_measurement_report: 5/6 chosen have it, 1/6 rejected have it -- clearly
# more likely to be chosen. But the feature does not actually win: rows
# WITH it average 0.4 beat_rate, rows WITHOUT it average 0.6.
for i in range(5):
    candidates.append({"title": f"{i}回比べたら分かったこと", "role": "chosen", "beat_rate": 0.4})
candidates.append({"title": "何も測っていない話", "role": "chosen", "beat_rate": 0.6})
candidates.append({"title": "9回比べた結果", "role": "rejected", "beat_rate": 0.4})
for i in range(5):
    candidates.append({"title": f"ただの感想 {i}", "role": "rejected", "beat_rate": 0.6})

stats = gh.compute_feature_stats("is_measurement_report", candidates, min_sample=5)
print(stats["status"], stats["chosen_rate"] > 0, stats["win_lift"] <= 0)
')"
check "a chosen-often, does-not-win feature is flagged" "flag" "$(echo "$OUT1" | awk '{print $1}')"
check "its chosen_rate is positive" "True" "$(echo "$OUT1" | awk '{print $2}')"
check "its win_lift is at or below zero" "True" "$(echo "$OUT1" | awk '{print $3}')"

# 2. A feature that is chosen often AND actually wins is NOT flagged.
OUT2="$(pyrun '
import goodhart as gh

candidates = []
# has_number: chosen often, same 5/6 vs 1/6 split as test 1 -- but this
# time the feature genuinely helps: WITH it averages 0.7, WITHOUT 0.3.
# Labels are letters, not a loop index, so the "without" titles do not
# accidentally contain a digit themselves.
for letter in "ABCDE":
    candidates.append({"title": f"{letter}0 tips that actually work", "role": "chosen", "beat_rate": 0.7})
candidates.append({"title": "General advice with no numbers", "role": "chosen", "beat_rate": 0.3})
candidates.append({"title": "Z9 tips nobody asked for", "role": "rejected", "beat_rate": 0.7})
for letter in "ABCDE":
    candidates.append({"title": f"Vague advice about {letter}", "role": "rejected", "beat_rate": 0.3})

stats = gh.compute_feature_stats("has_number", candidates, min_sample=5)
print(stats["status"], stats["chosen_rate"] > 0, stats["win_lift"] > 0)
')"
check "a chosen-often, actually-wins feature is NOT flagged" "ok" "$(echo "$OUT2" | awk '{print $1}')"
check "its chosen_rate is also positive (same selection pattern as test 1)" "True" "$(echo "$OUT2" | awk '{print $2}')"
check "but its win_lift is positive, which is what saves it from a flag" "True" "$(echo "$OUT2" | awk '{print $3}')"

# 3. Below the sample-size floor, goodhart reports "insufficient data"
#    rather than fabricating a flag from a handful of data points.
OUT3="$(pyrun '
import goodhart as gh

# Only 3 chosen and 3 rejected total -- well under the default floor of 5,
# even though the pattern LOOKS exactly like test 1 (would flag if trusted).
candidates = [
    {"title": "3回比べた結果", "role": "chosen", "beat_rate": 0.2},
    {"title": "5回比べた結果", "role": "chosen", "beat_rate": 0.2},
    {"title": "感想", "role": "chosen", "beat_rate": 0.9},
    {"title": "9回比べた結果", "role": "rejected", "beat_rate": 0.2},
    {"title": "ただの感想A", "role": "rejected", "beat_rate": 0.9},
    {"title": "ただの感想B", "role": "rejected", "beat_rate": 0.9},
]
stats = gh.compute_feature_stats("is_measurement_report", candidates, min_sample=5)
print(stats["status"])
')"
check "a sample below the floor reports insufficient_data, not a flag" "insufficient_data" "$(echo "$OUT3" | awk '{print $1}')"

# 4. The sample-size floor is configurable and the same data becomes
#    evaluable once the floor is lowered to match it.
OUT4="$(pyrun '
import goodhart as gh

candidates = [
    {"title": "3回比べた結果", "role": "chosen", "beat_rate": 0.2},
    {"title": "5回比べた結果", "role": "chosen", "beat_rate": 0.2},
    {"title": "感想", "role": "chosen", "beat_rate": 0.9},
    {"title": "9回比べた結果", "role": "rejected", "beat_rate": 0.2},
    {"title": "ただの感想A", "role": "rejected", "beat_rate": 0.9},
    {"title": "ただの感想B", "role": "rejected", "beat_rate": 0.9},
]
stats = gh.compute_feature_stats("is_measurement_report", candidates, min_sample=2)
print(stats["status"])
')"
check "the same data is evaluable once min_sample is lowered to fit" "flag" "$(echo "$OUT4" | awk '{print $1}')"

# A judge outage reports beat_rate 0.0 with zero scorable pairs. Feeding that
# into win_lift lets downtime depress whatever features those titles carried
# and manufacture a proxy flag out of an outage (spec 47 section 21.44).
mkdir -p "$TMP/outage-root/run-x/gates"
cat > "$TMP/outage-root/run-x/gates/beat-rate-en.json" <<'JSON'
{"run_id":"outage","lang":"en",
 "candidates":[{"title":"We measured 40 percent","role":"chosen","beat_rate":0.0,"scorable_pairs":0},
               {"title":"A plain headline","role":"rejected","beat_rate":0.5,"scorable_pairs":4}]}
JSON
OUT_OUTAGE=$(ROOT="$TMP/outage-root" SCRIPT_DIR="$SCRIPT_DIR" "$PY" -c '
import os, sys
from pathlib import Path
sys.path.insert(0, os.environ["SCRIPT_DIR"])
import goodhart
print(len(goodhart.load_candidates_from_runs(Path(os.environ["ROOT"]))))
')
check "an unscored candidate is excluded from the feature sample" "1" "$OUT_OUTAGE"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
