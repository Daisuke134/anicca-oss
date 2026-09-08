"""Twenty already-claimed rows are a reason to read the next twenty, not to stop.

A wake reads the union in slices of MAX_OPPORTUNITIES and has three turns to find work. The turn
loop continued only on `no_eligible_project`; `duplicate_project` -- every row in the slice was
already claimed -- ended the wake.

That was harmless while the union was about twenty rows, because there was no second slice.
Measured 2026-09-07, once category traversal took the union to roughly a hundred, five consecutive
wakes ended on `duplicate_project` with obs=20, having never looked at the other eighty.

Run: python3 -m pytest apps/lancers-revenue/tests/test_a_stale_slice_does_not_end_the_wake.py
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOOP = ROOT / "skills" / "earn" / "lancers" / "scripts" / "application_loop.py"


def _turn_guard() -> str:
    source = LOOP.read_text(encoding="utf-8")
    match = re.search(r"if result\.reason not in \(([^)]*)\):\s*\n\s*break", source)
    assert match, "the turn-continue guard is no longer recognisable"
    return match.group(1)


def test_an_all_claimed_slice_lets_the_wake_read_the_next_one():
    assert "duplicate_project" in _turn_guard()


def test_an_empty_slice_still_lets_the_wake_continue():
    """The original behaviour, which must not be lost while widening it."""
    assert "no_eligible_project" in _turn_guard()


def test_a_wake_that_found_work_still_stops():
    """Continuing after a submission would spend turns the lane does not need."""
    guard = _turn_guard()
    for outcome in ("submission_uncertain", "provider_terminal_blocked", "capacity_source_unavailable"):
        assert outcome not in guard, outcome


def test_the_wake_still_reads_in_slices_rather_than_one_oversized_batch():
    """build_planner_prompt rejects a batch over MAX_OPPORTUNITIES and fails every row in it."""
    source = LOOP.read_text(encoding="utf-8")
    assert "if len(fresh) >= MAX_OPPORTUNITIES: break" in source
    assert "if len(rows) > MAX_OPPORTUNITIES: raise ValueError" in source


def test_rows_already_read_this_wake_are_not_offered_again():
    """Without this the next turn would re-slice the same twenty forever."""
    source = LOOP.read_text(encoding="utf-8")
    assert "project_id not in wake_seen_ids" in source
    assert "wake_seen_ids.add(project_id)" in source
