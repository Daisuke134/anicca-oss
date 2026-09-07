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


# --- 求人 is not a proposal either, 2026-09-07 -----------------------------------------------

def test_a_recruitment_posting_never_reaches_the_submitter():
    """Measured across three Lancers category pages: 96 recruit badges against 52 projects and
    32 competitions. 求人 is the most common listing type there, it was normalising to "unknown"
    because the mapping had no entry for it, and so all 27 of the 30 cards on the system page
    reached the planner and came back mandatory_human_presence -- the twenty-row batch spent on
    job adverts. A hiring post is an application for employment, with interviews, not a proposal
    for a piece of work."""
    remaining, _claimed, skipped = _filter([_row("7", "recruit"), _row("8", "fixed")])
    assert [row["external_id"] for row in remaining] == ["8"]
    assert skipped[0]["reason"] == "unsupported_application_workflow"


def test_the_badge_text_lancers_prints_is_what_normalises():
    import importlib.util
    import sys
    path = ROOT / "skills" / "earn" / "lancers" / "scripts" / "status.py"
    spec = importlib.util.spec_from_file_location("lancers_status_worktype", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module._public_budget_type("求人") == "recruit"
    assert module._public_budget_type("プロジェクト") == "fixed"
    assert module._public_budget_type("コンペ") == "contest"
    assert module._public_budget_type("タスク") == "bounty"


def test_recruit_is_a_declared_budget_type_not_a_stray_string():
    """An undeclared value would fail contract validation and take the whole batch with it."""
    import json
    schema = json.loads(
        (ROOT / "skills" / "_shared" / "marketplace-core" / "schemas" / "opportunity.schema.json")
        .read_text(encoding="utf-8"))
    assert "recruit" in schema["properties"]["budget_type"]["enum"]
