"""Fifteen of the twenty catalogue listings were never searched on any given day.

Measured 2026-09-07 in production: `out_of_time: 15` on every wake. The lane wakes every 300s,
gets through about five listings before the 240s search budget runs out, and the rotation that
decides where it starts was `now.timetuple().tm_yday % len(listings)` -- the day of the year. It
advanced once a day, so the same five listings were searched from midnight to midnight and the
other fifteen were not looked at at all.

The counter that revealed this did not exist until the same afternoon; before that the fifteen
left silently and the wake reported a quiet board.

Run: python3 -m pytest apps/crowdworks-revenue/tests/test_the_whole_catalogue_gets_searched.py
"""

import datetime
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OWNER = ROOT / "skills" / "earn" / "crowdworks" / "scripts" / "application_owner.py"


def _owner():
    spec = importlib.util.spec_from_file_location("cw_rotation_under_test", OWNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BASE = datetime.datetime(2026, 9, 7, 19, 0, tzinfo=datetime.timezone.utc)


def _windows(module, listings, wakes):
    seen = set()
    for index in range(wakes):
        start = module._rotation(BASE + datetime.timedelta(seconds=module.WAKE_INTERVAL_SECONDS * index), listings)
        seen.update((start + offset) % len(listings) for offset in range(module.LISTINGS_READ_PER_WAKE))
    return seen


def test_every_listing_is_reached_within_one_pass():
    module = _owner()
    listings = module._listings()
    wakes = -(-len(listings) // module.LISTINGS_READ_PER_WAKE)
    assert _windows(module, listings, wakes) == set(range(len(listings)))


def test_consecutive_wakes_do_not_search_the_same_listings():
    """The whole fault was a rotation that stood still between wakes."""
    module = _owner()
    listings = module._listings()
    first = module._rotation(BASE, listings)
    second = module._rotation(BASE + datetime.timedelta(seconds=module.WAKE_INTERVAL_SECONDS), listings)
    assert first != second


def test_the_day_of_the_year_is_no_longer_the_rotation():
    source = OWNER.read_text(encoding="utf-8")
    assert "tm_yday" not in source


def test_an_unusable_clock_starts_at_the_beginning_rather_than_crashing():
    module = _owner()
    assert module._rotation(None, module._listings()) == 0
    assert module._rotation("not-a-time", module._listings()) == 0


def test_an_empty_catalogue_does_not_divide_by_zero():
    module = _owner()
    assert module._rotation(BASE, []) == 0
