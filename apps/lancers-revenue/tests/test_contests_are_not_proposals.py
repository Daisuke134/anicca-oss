"""A コンペ is not a proposal, and selecting one costs a whole submission.

Measured 2026-09-07 in production. `proposal_form_changed` was the largest single loss on the
Lancers lane -- 15 of 46 eligible projects across 120 wakes -- and once every raise site named
itself, all of it came from one line: `_production_prepare:479`, a `wait_for_function` on

    #FeeApp[data-work-id="<id>"]
      input[type=number][step=1000][max=100000000] exactly once
      input[type=text] exactly once

Opening the three failing postings showed why. They were logo contests, and a contest's proposal
page has no fee widget at all -- only 「data[Proposal][description]」, the new AI-declaration
radios and submit. There is no price to quote because the buyer picks from finished work
submitted on spec.

So this never was a broken selector. It was work the lane should not have selected, and
`budget_type` had said `contest` since discovery normalised it.

Run: python3 -m pytest apps/lancers-revenue/tests/test_contests_are_not_proposals.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
LOOP = ROOT / "skills" / "earn" / "lancers" / "scripts" / "application_loop.py"


def _loop():
    spec = importlib.util.spec_from_file_location("lancers_loop_contest_under_test", LOOP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(external_id, budget_type):
    return {
        "schema_version": "1.0.0", "record_type": "opportunity", "platform": "lancers",
        "external_id": external_id, "title": f"案件{external_id}", "description": "本文",
        "url": f"https://www.lancers.jp/work/detail/{external_id}", "category": "システム開発",
        "budget_type": budget_type, "budget_min_minor": 50000, "budget_max_minor": 100000,
        "currency": "JPY", "buyer_external_id": "b1", "observed_at": "2026-09-07T06:00:00+00:00",
    }


def _filter(rows):
    module = _loop()
    with tempfile.TemporaryDirectory() as directory, \
         patch.object(module.application_tick, "state_has_claim", return_value=False):
        return module._filter_claimed_rows(rows, Path(directory) / "application.json")


def test_a_contest_never_reaches_the_submitter():
    remaining, _claimed, skipped = _filter([_row("1", "contest"), _row("2", "fixed")])
    assert [row["external_id"] for row in remaining] == ["2"]
    assert {item["project_id"]: item["reason"] for item in skipped} == {
        "1": "unsupported_application_workflow"}


def test_a_bounty_is_still_excluded_as_it_always_was():
    remaining, _claimed, skipped = _filter([_row("3", "bounty")])
    assert remaining == []
    assert skipped[0]["reason"] == "unsupported_application_workflow"


def test_the_workable_budget_types_still_pass():
    remaining, _claimed, skipped = _filter([_row("4", "fixed"), _row("5", "hourly")])
    assert sorted(row["external_id"] for row in remaining) == ["4", "5"]
    assert skipped == []


def test_an_unknown_budget_type_is_not_refused_on_a_guess():
    """Refusing an unrecognised type would silently drop whatever Lancers names next."""
    remaining, _claimed, _skipped = _filter([_row("6", "unknown")])
    assert [row["external_id"] for row in remaining] == ["6"]
