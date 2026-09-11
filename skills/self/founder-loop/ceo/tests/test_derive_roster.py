"""test_derive_roster.py — RED (Phase 2a, feature claude-p-ceo-loop).
PROP-CEO-001 (B1修正, REQ-CEO-001): derive_roster(cadence_contract) -> list[str]. Type-filter
(only dict-valued keys are candidates) excludes `_comment` (str value) without a hardcoded key-name
exclusion list; `founder-loop` is dropped (CEO's own body).

ceo/allocator.py does not exist yet -> ImportError -> RED.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from allocator import derive_roster  # noqa: E402  RED: does not exist yet

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


def chk_true(name, cond):
    chk(name, bool(cond), True)


# Current cadence-contracts.json shape after retirement of the legacy Video slot.
fixture = {
    "_comment": "REQ-LV-100 — Cadence Contract declarations ...",
    "clip": {"kind": "row-exists"},
    "affiliate": {"kind": "row-exists"},
    "gig": {"kind": "row-exists"},
    "bounty": {"kind": "increment"},
    "founder-loop": {"kind": "pass-marker"},
    "pm-earner": {"kind": "compound", "conditions": []},
}

roster = derive_roster(fixture)
chk_true("derive_roster: _comment (str value) is excluded by the type filter", "_comment" not in roster)
chk_true("derive_roster: founder-loop (CEO's own body) is excluded", "founder-loop" not in roster)
chk("derive_roster: current fixture -> 5-item roster", len(roster), 5)
chk_true(
    "derive_roster: exact expected current set",
    set(roster) == {"clip", "affiliate", "gig", "bounty", "pm-earner"},
)

# The type filter must exclude _comment BEFORE any code path does contract["kind"] on it -- a str
# value has no ["kind"] key access, so calling derive_roster on this fixture must never raise
# TypeError/KeyError even though _comment's value is a plain string.
try:
    derive_roster(fixture)
    chk_true("derive_roster: does not raise on the _comment str-valued key (type filter runs first)", True)
except (TypeError, KeyError):
    chk_true("derive_roster: does not raise on the _comment str-valued key (type filter runs first)", False)

# No hardcoded loop-count assumption: adding a brand-new dict-valued loop key must show up
# automatically (roster is derived, never a fixed Python list).
fixture_plus_new_loop = dict(fixture)
fixture_plus_new_loop["article"] = {"kind": "row-exists"}
roster_plus = derive_roster(fixture_plus_new_loop)
chk_true("derive_roster: a newly-added dict-valued loop key is picked up automatically (no hardcoded list)", "article" in roster_plus)
chk("derive_roster: roster count grows by exactly 1 for the new loop", len(roster_plus), 6)

print(f"=== test_derive_roster: {P} passed {F} failed ===")
sys.exit(0 if F == 0 else 1)
