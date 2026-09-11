"""test_cadence_contracts_gig_source.py — RED (Phase 2a, feature gig-feasibility-volume).
PROP-031 / REQ-GFV-023 (rewritten, spec-review iteration-2 BLOCKING-1 fix) — static file check:
`__REPO_ROOT__/skills/self/cadence-contracts.json`'s `gig` entry, post-change, has `kind == "row-exists"`
(UNCHANGED — this REVERSES iteration-1's `increment` design after iteration-2 found it would be
structurally always-false against production reality) and an updated `source` string referencing
`applied.jsonl`/`listings.jsonl` (no longer `gig-funnel.jsonl`); every OTHER loop's entry
(`clip`,`affiliate`,`bounty`,`founder-loop`,`pm-earner`) is byte-for-byte unchanged.

Current `gig.source` is `"~/gig/gig-funnel.jsonl (REQ-LV-015)"` -> assertion fails -> RED.
"""
import json
import os
import sys

SELF_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACTS_PATH = os.path.join(SELF_DIR, "cadence-contracts.json")

P = 0
F = 0


def chk(name, got, want):
    global P, F
    if got == want:
        print(f"  ok {name} ({got})")
        P += 1
    else:
        print(f"  FAIL {name} want={want} got={got}")
        F += 1


with open(CONTRACTS_PATH, encoding="utf-8") as f:
    contracts = json.load(f)

gig = contracts["gig"]
chk("gig.kind is UNCHANGED: 'row-exists' (not 'increment')", gig["kind"], "row-exists")
chk("gig.source no longer references gig-funnel.jsonl",
    "gig-funnel.jsonl" in gig.get("source", ""), False)  # RED: current source IS gig-funnel.jsonl
chk("gig.source references applied.jsonl", "applied.jsonl" in gig.get("source", ""), True)
chk("gig.source references listings.jsonl", "listings.jsonl" in gig.get("source", ""), True)

# every other loop's entry must be byte-for-byte unchanged from the pre-feature baseline.
UNCHANGED_BASELINE = {
    "clip": {
        "kind": "row-exists", "cadence": "1/day", "unit": "reel", "boundary_tz": "Asia/Tokyo",
        "source": "CLIP_LEDGER ($EARN_LEDGER or ~/.local/state/life-manager/state/clip-earn-ledger.jsonl)",
    },
    "affiliate": {
        "kind": "row-exists", "cadence": "1/day", "unit": "reel", "boundary_tz": "Asia/Tokyo",
        "source": "~/.cloak/affiliate-metrics.jsonl (REQ-LV-014)",
    },
    "bounty": {
        "kind": "increment", "field": "checked",
        "source": "gated.json（bounty-funnel.jsonlのpass毎の値から前日比を算出）",
        "boundary_tz": "Asia/Tokyo",
    },
    "founder-loop": {
        # BUG FIX 2026-07-11: source path corrected from ".../state/STATE.md" (never matched
        # where founder-loop.sh actually writes) to ".../STATE.md" — see cadence-evidence.py's
        # _founder_state_md_path() comment for the full root-cause trace.
        "kind": "pass-marker", "source": "~/.local/state/life-manager/founder-loop-cadence/STATE.md mtime",
        "boundary_tz": "Asia/Tokyo",
    },
    "pm-earner": {
        "kind": "compound",
        "conditions": [
            {"id": "hourly-pass", "kind": "recency", "cadence": "1/hour", "source": "earner.log mtime", "max_age_min": 40},
            {"id": "daily-redeem", "kind": "row-exists", "cadence": "1/day", "unit": "redeem", "boundary_tz": "Asia/Tokyo"},
        ],
    },
}
for loop, expected in UNCHANGED_BASELINE.items():
    chk(f"{loop} entry is byte-for-byte unchanged", contracts[loop], expected)

print(f"=== test_cadence_contracts_gig_source: {P} passed {F} failed ===")
sys.exit(0 if F == 0 else 1)
