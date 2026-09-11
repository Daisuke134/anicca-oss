"""test_cadence_evidence_gig_branch.py — RED (Phase 2a, feature gig-feasibility-volume).
PROP-032 / REQ-GFV-023 (rewritten, spec-review iteration-2 BLOCKING-1 fix) — end-to-end integration
test through the REAL `status_for_loop("gig")` dispatcher, proving `gig` has been moved OUT of the
shared `("clip","affiliate","video","gig")` row-exists tuple and INTO its own
`_gig_activity_event_dates`-backed branch that reads `applied.jsonl`+`listings.jsonl` (via the NEW
`GIG_APPLIED_PATH`/`GIG_LISTINGS_PATH` env-override seams, mirroring the existing
EARN_LEDGER/AFFILIATE_METRICS_PATH/GIG_FUNNEL_PATH per-loop override convention already used by this
same module's other loops).

This is a NEW file, deliberately NOT an edit to the existing `tests/test_cadence_evidence.py`
(kept byte-for-byte untouched — it remains the green regression baseline for clip/affiliate/video/
bounty/founder-loop/pm-earner, whose GIG_FUNNEL_PATH-based `gig` assertion at its line 79-84 this
feature's redesign intentionally supersedes for the `gig` loop specifically). Mirrors that file's
own fixture-writing style (`write_jsonl` helper, temp dir, env var set/unset per case) — same
"extends the existing fixture pattern" instruction the spec calls for, applied via a sibling file
rather than an in-place edit, so the OLD file's own `gig`-via-`GIG_FUNNEL_PATH` case stays provably
green throughout (it exercises the pre-existing, still-valid `_row_exists_event_dates` code path,
which this feature does not delete — see verification-architecture.md §2 PROP-031's Edge Cases).

`cadence-evidence.py` still routes `gig` through the shared `_row_exists_event_dates("gig")`/
`GIG_FUNNEL_PATH` path (the OLD design) -> these new env vars are never read -> assertions fail ->
RED.
"""
import datetime
import importlib.util
import json
import os
import sys
import zoneinfo

_SELF_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SELF_DIR)
_spec = importlib.util.spec_from_file_location("cadence_evidence", os.path.join(_SELF_DIR, "cadence-evidence.py"))
CE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CE)

JST = zoneinfo.ZoneInfo("Asia/Tokyo")

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


def write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


import tempfile  # noqa: E402

TMP = tempfile.mkdtemp()
TODAY_ISO = datetime.datetime.now(tz=JST).date().isoformat()
today_noon_iso = f"{TODAY_ISO}T03:00:00Z"  # 03:00 UTC = 12:00 JST, safely mid-day for TODAY_ISO

# --- Case 1: a real "applied"-status row today -> cadence_met() returns True ---
applied_path = os.path.join(TMP, "applied.jsonl")
listings_path = os.path.join(TMP, "listings.jsonl")
write_jsonl(applied_path, [
    {"ts": today_noon_iso, "requestId": "R1", "status": "applied", "category": "PPT/スライド"},
    {"ts": today_noon_iso, "status": "no_action"},
])
write_jsonl(listings_path, [])
os.environ["GIG_APPLIED_PATH"] = applied_path
os.environ["GIG_LISTINGS_PATH"] = listings_path
status = CE.status_for_loop("gig")  # RED: still reads GIG_FUNNEL_PATH, ignores these new env vars
chk("gig (new source): real today 'applied'-status row -> met=true", status["met"], True)
del os.environ["GIG_APPLIED_PATH"]
del os.environ["GIG_LISTINGS_PATH"]

# --- Case 2: ONLY housekeeping-status rows today -> cadence_met() returns False ---
write_jsonl(applied_path, [
    {"ts": today_noon_iso, "status": "no_action"},
    {"ts": today_noon_iso, "status": "applied_0"},
])
os.environ["GIG_APPLIED_PATH"] = applied_path
os.environ["GIG_LISTINGS_PATH"] = listings_path
status = CE.status_for_loop("gig")
chk("gig (new source): ONLY housekeeping-status rows today -> met=false (honest, not fabricated)",
    status["met"], False)
del os.environ["GIG_APPLIED_PATH"]
del os.environ["GIG_LISTINGS_PATH"]

# --- Case 3: mixed-format ts (ISO string + numeric epoch) on the SAME day -> both contribute
# correctly to the same date, no duplicate/missed date.
today_epoch_noon = datetime.datetime.now(tz=JST).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
write_jsonl(applied_path, [
    {"ts": today_noon_iso, "requestId": "R1", "status": "applied"},       # ISO string
    {"ts": today_epoch_noon, "requestId": "R2", "status": "replied"},     # numeric epoch, same day
])
os.environ["GIG_APPLIED_PATH"] = applied_path
os.environ["GIG_LISTINGS_PATH"] = listings_path
status = CE.status_for_loop("gig")
chk("gig (new source): mixed-format ts (ISO + numeric epoch) same day -> met=true, no crash",
    status["met"], True)
del os.environ["GIG_APPLIED_PATH"]
del os.environ["GIG_LISTINGS_PATH"]

print(f"=== test_cadence_evidence_gig_branch: {P} passed {F} failed ===")
sys.exit(0 if F == 0 else 1)
