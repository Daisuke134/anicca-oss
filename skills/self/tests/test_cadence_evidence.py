"""test_cadence_evidence.py — Tier2 integration test for cadence-evidence.py (REQ-LV-104 §H,
sprint-2 gap noted in Phase 5 hardening review). cadence.py itself (cadence_met/streak) is pure and
already covered by test_cadence.py's Tier1 suite (23/23 green, untouched here). This file proves
the IMPURE evidence-gathering side (gather_evidence/evidence_by_date_for_streak/status_for_loop)
correctly reads REAL temp fixture files — via the module's own test-only env var override seams
(EARN_LEDGER/AFFILIATE_METRICS_PATH/EARN_VIDEO_METRICS_PATH/GIG_FUNNEL_PATH/BOUNTY_FUNNEL_PATH/
FOUNDER_STATE_MD_PATH/PM_EARNER_LOG_PATH/PM_EARNER_LEDGER_PATH — the same
EARN_LEDGER/FOUNDER_DIR/FOUNDER_TEST pattern this codebase already uses elsewhere) — never touches
production paths (~/.cloak, ~/gig, ~/.anicca-founder, __REPO_ROOT__/skills/earn/...).
"""
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import time
import zoneinfo

_SELF_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _SELF_DIR)  # so cadence-evidence.py's own `from cadence import ...` resolves
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


TMP = tempfile.mkdtemp()
TODAY = datetime.datetime.now(tz=JST).date().isoformat()
today_epoch = datetime.datetime.now(tz=JST).timestamp()

# ---------------------------------------------------------------------------
# row-exists loops (clip/affiliate/gig): real jsonl row with today's ts -> event_dates
# includes today -> cadence_met via the real cadence.py dispatcher.
# ---------------------------------------------------------------------------
clip_ledger = os.path.join(TMP, "clip-ledger.jsonl")
write_jsonl(clip_ledger, [{"ts": today_epoch, "post_url": "https://example.com/reel1"}])
os.environ["EARN_LEDGER"] = clip_ledger
status = CE.status_for_loop("clip")
chk("clip: real today-ts row -> met=true", status["met"], True)
chk("clip: real today-ts row -> streak>=1", status["streak"] >= 1, True)
del os.environ["EARN_LEDGER"]

aff_metrics = os.path.join(TMP, "affiliate-metrics.jsonl")
write_jsonl(aff_metrics, [{"ts": today_epoch, "views": 100}])
os.environ["AFFILIATE_METRICS_PATH"] = aff_metrics
status = CE.status_for_loop("affiliate")
chk("affiliate: real today-ts row -> met=true", status["met"], True)
del os.environ["AFFILIATE_METRICS_PATH"]

# FIX (pre-existing baseline failure, confirmed present on origin/main before this file's other
# 2026-07-11 edits — see ab4a39a4's own commit message): this assertion used to set GIG_FUNNEL_PATH,
# but cadence-evidence.py's gig evidence source moved to GIG_APPLIED_PATH/GIG_LISTINGS_PATH
# (_gig_row_exists_event_dates, REQ-GFV-022/023 — _gig_funnel_path() itself documents "no longer
# used by the `gig` cadence-evidence path below"). The old fixture therefore always fed an env var
# gather_evidence("gig") never reads, so this always failed regardless of real gig behavior (which
# IS correctly covered end-to-end by test_cadence_evidence_gig_branch.py / test_gig_cadence_real_
# evidence.py, both green). Updated to the CURRENT override seam so this assertion exercises the
# real code path again, needed for the self-heal.md FIND-001 pre-push gate (skills/self/tests/
# test_cadence_evidence.py must genuinely pass end-to-end for that gate to be usable at all).
gig_applied = os.path.join(TMP, "gig-applied.jsonl")
gig_listings = os.path.join(TMP, "gig-listings.jsonl")
write_jsonl(gig_applied, [{"ts": today_epoch, "requestId": "R1", "status": "applied"}])
write_jsonl(gig_listings, [])
os.environ["GIG_APPLIED_PATH"] = gig_applied
os.environ["GIG_LISTINGS_PATH"] = gig_listings
status = CE.status_for_loop("gig")
chk("gig: real today-ts 'applied' row (current GIG_APPLIED_PATH source) -> met=true", status["met"], True)
del os.environ["GIG_APPLIED_PATH"]
del os.environ["GIG_LISTINGS_PATH"]

# ---------------------------------------------------------------------------
# bounty (increment kind): today's checked value must be STRICTLY greater than the prior day's,
# not merely present — proves the real bounty-funnel.jsonl reader feeds cadence.py's increment
# branch correctly (this is the exact "row exists != healthy" bug class the whole feature exists
# to fix, now proven end-to-end through the real file reader, not just the pure dispatcher).
# ---------------------------------------------------------------------------
yesterday = (datetime.date.fromisoformat(TODAY) - datetime.timedelta(days=1)).isoformat()
yesterday_epoch = datetime.datetime.combine(
    datetime.date.fromisoformat(yesterday), datetime.time(12, 0), tzinfo=JST
).timestamp()
bounty_funnel = os.path.join(TMP, "bounty-funnel.jsonl")
write_jsonl(bounty_funnel, [
    {"ts": yesterday_epoch, "checked": 10},
    {"ts": today_epoch, "checked": 15},
])
os.environ["BOUNTY_FUNNEL_PATH"] = bounty_funnel
status = CE.status_for_loop("bounty")
chk("bounty: checked increased 10->15 -> met=true", status["met"], True)
del os.environ["BOUNTY_FUNNEL_PATH"]

write_jsonl(bounty_funnel, [
    {"ts": yesterday_epoch, "checked": 10},
    {"ts": today_epoch, "checked": 10},
])
os.environ["BOUNTY_FUNNEL_PATH"] = bounty_funnel
status = CE.status_for_loop("bounty")
chk("bounty: checked flat 10->10 (row exists, no increment) -> met=false", status["met"], False)
del os.environ["BOUNTY_FUNNEL_PATH"]

# ---------------------------------------------------------------------------
# founder-loop (pass-marker kind): STATE.md's real mtime, ledger NOT consulted (proves the "empty
# ledger day is not the same as pass-didn't-run" design decision holds through the real file
# reader too).
# ---------------------------------------------------------------------------
founder_state = os.path.join(TMP, "STATE.md")
os.makedirs(os.path.dirname(founder_state), exist_ok=True)
with open(founder_state, "w") as f:
    f.write("realised_earn_usdc: 0\n")  # zero-earn day; mtime alone must still count as "ran"
os.environ["FOUNDER_STATE_MD_PATH"] = founder_state
status = CE.status_for_loop("founder-loop")
chk("founder-loop: STATE.md written today (even zero-earn) -> met=true", status["met"], True)
del os.environ["FOUNDER_STATE_MD_PATH"]

# ---------------------------------------------------------------------------
# pm-earner (compound: recency AND row-exists): real earner.log mtime (just-touched -> within
# max_age_min) AND a real today-ts redeem row in the ledger -> both sub-conditions true -> met.
# ---------------------------------------------------------------------------
earner_log = os.path.join(TMP, "earner.log")
with open(earner_log, "w") as f:
    f.write("pass ok\n")
os.utime(earner_log, (time.time(), time.time()))  # just-touched, well within the 40min window
pm_ledger = os.path.join(TMP, "pm-earn-ledger.jsonl")
write_jsonl(pm_ledger, [{"ts": today_epoch, "source": "polymarket-redeem", "amount": 1.5}])
os.environ["PM_EARNER_LOG_PATH"] = earner_log
os.environ["PM_EARNER_LEDGER_PATH"] = pm_ledger
status = CE.status_for_loop("pm-earner")
chk("pm-earner: fresh earner.log AND today redeem row -> met=true (compound AND)", status["met"], True)
del os.environ["PM_EARNER_LOG_PATH"]
del os.environ["PM_EARNER_LEDGER_PATH"]

# pm-earner with a STALE earner.log (old mtime) but a real today redeem row -> compound AND must
# still fail (hourly-pass sub-condition false) — this is PROP-LV-040/G1's exact regression,
# end-to-end through the real evidence-gathering path this time.
old_log = os.path.join(TMP, "earner-stale.log")
with open(old_log, "w") as f:
    f.write("pass ok\n")
old_time = time.time() - 3600 * 3  # 3h ago, well past the 40min max_age_min
os.utime(old_log, (old_time, old_time))
os.environ["PM_EARNER_LOG_PATH"] = old_log
os.environ["PM_EARNER_LEDGER_PATH"] = pm_ledger
status = CE.status_for_loop("pm-earner")
chk("pm-earner: STALE earner.log (3h) AND today redeem row -> met=false (AND, not OR — G1 regression, end-to-end)",
    status["met"], False)
del os.environ["PM_EARNER_LOG_PATH"]
del os.environ["PM_EARNER_LEDGER_PATH"]

print(f"=== test_cadence_evidence: {P} passed {F} failed ===")
sys.exit(0 if F == 0 else 1)
