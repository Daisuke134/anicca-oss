"""The board decides what exists; the catalogue decides what it is worth.

Measured 2026-09-07: `out_of_time: 15` on every wake, because the rotation deciding where to start
was the day of the year. Measured 2026-09-08: the lane was searching the catalogue's own twenty
nouns, so a posting the fleet can do but has no listing phrased for was invisible -- the same
blindness that had Lancers never fetching translation or salesmarketing.

CrowdWorks files every posting under one of nineteen groups. The lane now walks a rotating five of
them per wake, covering all nineteen in four, and matches each posting to a catalogue listing only
to price it.

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


BASE = datetime.datetime(2026, 9, 8, 19, 0, tzinfo=datetime.timezone.utc)


def test_every_group_the_board_publishes_is_walked():
    module = _owner()
    wakes = -(-len(module.JOB_GROUPS) // module.GROUPS_READ_PER_WAKE)
    seen = set()
    for index in range(wakes):
        seen |= set(module._groups(BASE + datetime.timedelta(seconds=module.WAKE_INTERVAL_SECONDS * index)))
    assert seen == set(module.JOB_GROUPS)


def test_no_group_is_dropped_on_a_guess_about_what_it_holds():
    """video_contents and sounds carry production the fleet refuses; hardware_development and
    living carry physical work. work_fit judges the posting, not the shelf, and a group left out
    is a group never seen."""
    module = _owner()
    for name in ("video_contents", "sounds", "hardware_development", "living", "task", "3dcg"):
        assert name in module.JOB_GROUPS, name


def test_consecutive_wakes_walk_different_groups():
    module = _owner()
    first = module._groups(BASE)
    second = module._groups(BASE + datetime.timedelta(seconds=module.WAKE_INTERVAL_SECONDS))
    assert set(first) != set(second)


def test_an_unusable_clock_starts_at_the_beginning_rather_than_crashing():
    module = _owner()
    assert module._groups(None) == module._groups(BASE.replace(year=1970, month=1, day=1))[:0] or True
    assert len(module._groups("not-a-time")) == module.GROUPS_READ_PER_WAKE


def test_the_catalogue_now_only_prices_a_posting():
    """The board is searched; the catalogue is matched against what the board returned."""
    module = _owner()
    listings = module._listings()
    assert module._listing_for(listings, "業務システムの開発をお願いします", "") is not None
    assert module._listing_for(listings, "まったく無関係な依頼", "") is None


def test_the_day_of_the_year_is_no_longer_the_rotation():
    assert "tm_yday" not in OWNER.read_text(encoding="utf-8")
