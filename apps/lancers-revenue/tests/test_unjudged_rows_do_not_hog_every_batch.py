"""A row the planner declined to judge is not a row it refused, and it must not come back forever.

Measured 2026-09-07: across eight consecutive wakes, 15 of the decision reports were `invalid` and
they were the same postings every time -- two clinic logos, a bar logo, 「限定公開の仕事」. An
unjudged row is cached nowhere, so it returned every minute and spent part of the twenty-row
planner budget on work no decision was ever reached about, while `eligible_count` stayed 0.

Refusals are cached for a week because a refusal is a decision. An unjudged row gets an hour:
long enough to stop it crowding the batch, short enough that a transient miss is retried the same
afternoon rather than lost until next week.

Run: python3 -m pytest apps/lancers-revenue/tests/test_unjudged_rows_do_not_hog_every_batch.py
"""

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOOP = ROOT / "skills" / "earn" / "lancers" / "scripts" / "application_loop.py"


def _loop():
    spec = importlib.util.spec_from_file_location("lancers_loop_unjudged_under_test", LOOP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _row(external_id):
    return {
        "schema_version": "1.0.0", "record_type": "opportunity", "platform": "lancers",
        "external_id": external_id, "title": f"案件{external_id}", "description": "本文",
        "url": f"https://www.lancers.jp/work/detail/{external_id}", "category": "システム開発",
        "budget_type": "fixed", "budget_min_minor": 50000, "budget_max_minor": 100000,
        "currency": "JPY", "buyer_external_id": "b1", "observed_at": "2026-09-07T06:00:00+00:00",
    }


def _cache_after(unjudged=(), decisions=None):
    module = _loop()
    rows = {"1": _row("1"), "2": _row("2")}
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory) / "application.json"
        module._cache_no_effect(decisions or {}, rows, state, unjudged)
        return module, json.loads(module._skip_cache_path(state).read_text(encoding="utf-8"))


def test_an_unjudged_row_is_held_back_so_the_next_batch_has_room():
    _module, cache = _cache_after(unjudged=("1",))
    entries = cache.get("decisions", cache)
    assert "1" in entries and entries["1"]["business_class"] == "unjudged"
    assert "2" not in entries


def test_it_is_held_for_an_hour_not_a_week():
    """A refusal is a decision and keeps its week; a miss is not, and must be retried today."""
    module, cache = _cache_after(unjudged=("1",))
    entries = cache.get("decisions", cache)
    remaining = float(entries["1"]["expires_at"]) - time.time()
    assert 0 < remaining <= module.UNJUDGED_CACHE_TTL_SECONDS + 5
    assert module.UNJUDGED_CACHE_TTL_SECONDS == 3600
    assert module.SKIP_CACHE_TTL_SECONDS > module.UNJUDGED_CACHE_TTL_SECONDS


def test_a_real_refusal_is_never_downgraded_to_the_short_window():
    """If a row is both refused and listed as unjudged, the refusal wins."""
    module, cache = _cache_after(
        unjudged=("1",),
        decisions={"1": {"business_class": "hard_prohibited", "reason_codes": ["physical_or_onsite"]}})
    entries = cache.get("decisions", cache)
    assert entries["1"]["business_class"] == "hard_prohibited"
    assert float(entries["1"]["expires_at"]) - time.time() > module.UNJUDGED_CACHE_TTL_SECONDS


def test_an_id_with_no_row_is_ignored_rather_than_cached_blind():
    _module, cache = _cache_after(unjudged=("999",))
    assert "999" not in cache.get("decisions", cache)


# --- the cache had never worked at all, 2026-09-07 -------------------------------------------

def test_the_content_hash_can_actually_be_computed():
    """`_skip_content_sha256` calls hashlib, which was never imported. Every call raised
    NameError, `_cache_no_effect` is wrapped in `except Exception: pass`, and so the production
    cache was `{"decisions": {}}` -- refusals were re-judged by the planner every single wake,
    forever, and the twenty-row batch filled with work already refused."""
    module = _loop()
    digest = module._skip_content_sha256(_row("1"))
    assert isinstance(digest, str) and len(digest) == 64


def test_a_refusal_actually_lands_in_the_file():
    _module, cache = _cache_after(
        decisions={"1": {"business_class": "hard_prohibited", "reason_codes": ["physical_or_onsite"]}})
    entries = cache.get("decisions", cache)
    assert entries["1"]["business_class"] == "hard_prohibited"
    assert len(entries["1"]["content_sha256"]) == 64


def test_the_cache_write_is_not_allowed_to_fail_silently_in_tests():
    """The production failure was invisible because the only caller swallows every exception.
    Calling _cache_no_effect directly, as these tests do, is what makes it visible."""
    module = _loop()
    import inspect
    source = inspect.getsource(module)
    assert "_cache_no_effect(decisions, rows_by_id, state_path, invalid_ids)" in source
