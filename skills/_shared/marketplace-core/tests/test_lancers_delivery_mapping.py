"""The catalogue holds what the work takes; each platform's projection adapts to its form.

Five catalogue tiers are quoted at 18 or 25 days. Lancers accepts only a fixed set of
delivery times and rejects anything else outright, so four families -- ai_agent_integration,
web_scraping_tool, spreadsheet_replace_system, chrome_extension_dev -- could not become
listings there at all.

Editing the catalogue to fit one platform would be the wrong repair. Coconala reads the same
rows, so a number changed for Lancers would silently change what Coconala sells. The
projection adapts instead, and rounds upward: a buyer told 21 days and delivered in 18 got
their work early; a buyer told 14 was promised something the catalogue never said we could do.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3].parent
SPEC = importlib.util.spec_from_file_location(
    "_lc_delivery",
    Path(__file__).resolve().parents[1] / "scripts" / "listing_catalog.py")
LC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LC)

CATALOG = LC.load(Path(__file__).resolve().parents[3] / "gig-work" / "profile" / "listings" / "catalog.json")
ALLOWED = set(LC.LANCERS_DELIVERY_DAYS)


@pytest.mark.parametrize("family", sorted(LC.entries_by_family(CATALOG)))
def test_every_family_now_maps_to_a_delivery_time_lancers_accepts(family):
    for plan in LC.project_lancers(CATALOG, family)["plans"]:
        assert plan["delivery_days"] in ALLOWED, (family, plan)


@pytest.mark.parametrize("given, expected", [(18, 21), (25, 30), (8, 10), (100, 90)])
def test_an_unaccepted_time_rounds_up(given, expected):
    assert LC._lancers_delivery_days(given) == expected


@pytest.mark.parametrize("given", sorted(ALLOWED))
def test_an_accepted_time_is_left_alone(given):
    assert LC._lancers_delivery_days(given) == given


def test_it_never_rounds_down():
    for days in range(1, 95):
        mapped = LC._lancers_delivery_days(days)
        assert mapped >= days or days > max(ALLOWED)


def test_a_non_integer_is_returned_untouched_for_the_validator_to_refuse():
    # Guessing a number for a malformed tier would hide the malformation.
    for value in (None, "10", 10.5):
        assert LC._lancers_delivery_days(value) == value


def test_the_adjustment_is_reported_not_silent():
    result = LC.project_lancers(CATALOG, "spreadsheet_replace_system")
    adjusted = {(row["catalog_days"], row["lancers_days"]) for row in result["delivery_days_adjusted"]}
    assert adjusted == {(18, 21), (25, 30)}


def test_a_family_that_needed_no_adjustment_reports_none():
    assert LC.project_lancers(CATALOG, "line_bot_dev")["delivery_days_adjusted"] == []


def test_the_catalogue_itself_still_says_eighteen():
    # The repair must not have edited the shared truth.
    tiers = LC.entries_by_family(CATALOG)["chrome_extension_dev"]["tiers"]
    assert 18 in [tier["delivery_days"] for tier in tiers]
