"""The board publishes its own categories, and asking it beats guessing nouns.

Measured 2026-09-07 against the live board with type[]=project:

    keyword 業務システム    3 postings
    /system               23
    /web                  17
    /business             12
    /writing              30
    /design               23

Five category requests therefore see about three times what twelve keyword requests do, at under
half the request rate -- which matters, because probing a marketplace harder than it expects is
how the Coconala session earned a 403 the same morning. The keywords are kept: a category is
broad, and the catalogue nouns still reach postings filed somewhere unexpected.

Run: python3 -m pytest apps/lancers-revenue/tests/test_categories_beat_keywords.py
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
LOOP = ROOT / "skills" / "earn" / "lancers" / "scripts" / "application_loop.py"
STATUS = ROOT / "skills" / "earn" / "lancers" / "scripts" / "status.py"


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_every_category_is_asked_before_any_keyword():
    """A thin keyword slice must not decide the wake."""
    loop = _module("lancers_loop_categories", LOOP)
    order = []

    def discover(**kwargs):
        order.append(kwargs.get("category") or kwargs.get("query"))
        return {"ok": False, "error": "no_normalized_opportunities", "opportunities": []}

    with patch.object(loop.status, "run_discovery", side_effect=discover):
        loop._run_exhaustive_discovery(20.0)

    assert order[:len(loop.DISCOVERY_CATEGORIES)] == list(loop.DISCOVERY_CATEGORIES)
    assert len(order) == len(loop.DISCOVERY_CATEGORIES) + len(loop.DISCOVERY_QUERIES)


def test_a_category_request_carries_no_keyword():
    """Sending both would narrow the category back down to the keyword's three rows."""
    loop = _module("lancers_loop_categories_args", LOOP)
    seen = []

    def discover(**kwargs):
        seen.append(kwargs)
        return {"ok": False, "error": "no_normalized_opportunities", "opportunities": []}

    with patch.object(loop.status, "run_discovery", side_effect=discover):
        loop._run_exhaustive_discovery(20.0)

    for call in seen[:len(loop.DISCOVERY_CATEGORIES)]:
        assert call["query"] is None and call["category"] in loop.DISCOVERY_CATEGORIES


def test_the_categories_are_ones_the_fleet_can_serve():
    loop = _module("lancers_loop_categories_names", LOOP)
    assert set(loop.DISCOVERY_CATEGORIES) == {"system", "web", "business", "writing", "design"}


def test_the_url_builder_puts_the_category_in_the_path():
    """Lancers' facet is a path, not a parameter."""
    status = _module("lancers_status_categories", STATUS)
    captured = {}

    def fake_open(request, timeout=None):
        captured["url"] = request.full_url
        raise RuntimeError("stop before the network")

    with patch.object(status.urllib.request, "urlopen", side_effect=fake_open):
        try:
            status.fetch_public_html(query=None, limit=20, timeout=5.0, category="system")
        except Exception:
            pass
    assert "/work/search/system?" in captured["url"]
    assert "type%5B%5D=project" in captured["url"]


def test_a_category_that_is_not_a_path_segment_is_refused():
    """The category lands in a URL path, so it may not carry a slash or a query."""
    status = _module("lancers_status_categories_guard", STATUS)
    for bad in ("../admin", "system?x=1", "system/extra", "", "SYSTEM", 5):
        try:
            status._category(bad)
        except Exception:
            continue
        raise AssertionError(f"accepted {bad!r}")
    assert status._category("system") == "system"
    assert status._category(None) is None
