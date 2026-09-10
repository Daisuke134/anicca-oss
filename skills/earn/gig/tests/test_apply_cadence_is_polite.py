"""Apply wakes every five minutes after the provider clarified the restriction cause.

Provider support attributes the restriction to an earlier system cancellation caused by client
non-contact, not to application cadence. The former 30-minute guard encoded the disproven
throttling hypothesis and delayed new opportunities. Five minutes keeps one bounded, idempotent
wake while avoiding the old one-minute polling interval.

Run: python3 -m pytest skills/earn/gig/tests/test_apply_cadence_is_polite.py
"""

import json
from pathlib import Path

REGISTRY = json.loads((Path(__file__).resolve().parents[4] / "config" / "loop-registry.json")
                      .read_text(encoding="utf-8"))


def _lane(name):
    def find(node):
        if isinstance(node, dict):
            if name in node:
                return node[name]
            for value in node.values():
                found = find(value)
                if found is not None:
                    return found
        return None
    lane = find(REGISTRY)
    assert lane is not None, f"{name} is not in the registry"
    return lane


def test_apply_wakes_every_five_minutes():
    assert _lane("hf-gig-apply-direct")["cadence"]["start_interval_seconds"] == 300


def test_it_is_not_back_to_one_minute_polling():
    assert _lane("hf-gig-apply-direct")["cadence"]["start_interval_seconds"] > 60


def test_only_the_applying_lane_changes():
    assert _lane("hf-gig-storefront-direct")["cadence"]["start_interval_seconds"] == 60
    assert _lane("hf-gig-paid-direct")["cadence"]["start_interval_seconds"] == 300
