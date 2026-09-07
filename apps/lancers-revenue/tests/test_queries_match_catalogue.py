"""Lancers must search for what the catalogue sells.

Measured 2026-09-07: Lancers applied to nothing all day. Not a form problem and not a session
problem -- the planner judged 10 fresh projects per wake and found zero it could honestly take.
The last 60 decisions were 17 `mandatory_attribute_fabrication`, 14 `mandatory_human_presence`,
14 `video_or_animation`, against a board of short-video editing, on-site filming in 錦糸町, and
Threads management.

The lane had fetched that board itself. Half of `DISCOVERY_QUERIES` was SNS and content marketing
("SNS運用", "SNS投稿", "コンテンツ制作", "X運用", "B2Bマーケティング") while all 20 catalogue
listings are system and automation build work. One query runs per pass, so most passes saw nothing
sellable. The lane was fetching work it is honest enough to refuse.

Run: python3 -m pytest apps/lancers-revenue/tests/test_queries_match_catalogue.py
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LOOP = ROOT / "skills" / "earn" / "lancers" / "scripts" / "application_loop.py"
CATALOG = ROOT / "skills" / "gig-work" / "profile" / "listings" / "catalog.json"


def _queries():
    spec = importlib.util.spec_from_file_location("lancers_loop_under_test", LOOP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.DISCOVERY_QUERIES


def _catalogue_text() -> str:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else (data.get("listings") or data.get("items") or [])
    parts = []
    for item in items:
        parts.append(str(item.get("title_ja") or ""))
        parts.append(str(item.get("value_prop") or ""))
        parts.append(str(item.get("family") or "").replace("_", " "))
    return " ".join(parts)


# 2026-09-07, second pass. The rule above used to be "every query must appear in the catalogue",
# which is stricter than the reason it was written for. The reason was that half the queries named
# work the planner is obliged to refuse; the catalogue happened to be a convenient stand-in for
# "work we can take". It is not the same thing. Measured the same day, the twelve catalogue-backed
# queries saw eighteen postings across the whole board, every judgeable one unworkable, and the
# lane reported no_eligible_project every minute while being entirely correct. Dais: "they prefer
# 開発 but it's not the only thing they can work on ... buyma not good and sns posting itself and
# physical shit but all others they could do".
#
# So the rule is now the reason: a query may not name work that work_fit.py refuses. Pricing does
# not depend on the catalogue -- the planner sets price_jpy within the posting's own budget -- so
# a non-catalogue query produces a real proposal, not a broken one.

PROHIBITED_QUERY_WORDS = (
    "動画", "映像", "アニメ", "撮影", "ナレーション", "声優", "テレアポ", "コールセンター",
    "出品代行", "せどり", "転売", "BUYMA", "バイマ", "投稿代行", "SNS運用", "SNS投稿",
    "配達", "配送", "梱包", "清掃", "内職", "軽作業", "現地", "出張",
)


@pytest.mark.parametrize("query", _queries())
def test_no_query_fetches_work_the_fleet_must_refuse(query):
    """Searching for work we decline only manufactures skips; that is what this file exists for."""
    for word in PROHIBITED_QUERY_WORDS:
        assert word not in query, f"'{query}' names {word}, which work_fit.py refuses"


def test_the_build_queries_the_catalogue_backs_are_all_still_there():
    """Widening must not drop what was already earning."""
    for query in ("業務自動化", "業務システム", "Webアプリ", "システム開発", "LINE Bot",
                  "スクレイピング", "Excel VBA", "ダッシュボード", "Chrome拡張", "RPA",
                  "ECサイト", "不具合修正"):
        assert query in _queries()


def test_a_wake_reads_a_rotating_slice_rather_than_the_whole_vocabulary():
    """Adding queries must not multiply the request rate on a board polled every 60 seconds --
    probing harder than a marketplace expects is how the Coconala session earned a 403."""
    spec = importlib.util.spec_from_file_location("lancers_loop_window", LOOP)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.DISCOVERY_WINDOW <= 12
    assert len(module.DISCOVERY_QUERIES) > module.DISCOVERY_WINDOW
    from datetime import datetime, timezone
    windows = {module._discovery_window(datetime(2026, 8, 13, hour, minute, tzinfo=timezone.utc))
               for hour in range(24) for minute in (0, 30)}
    assert all(len(window) == module.DISCOVERY_WINDOW for window in windows)
    # Every query is reachable, so the vocabulary is covered over time rather than in one wake.
    assert set().union(*windows) == set(module.DISCOVERY_QUERIES)


@pytest.mark.parametrize("banned", [
    "SNS運用", "SNS投稿", "コンテンツ制作", "X運用", "B2Bマーケティング",
])
def test_the_marketing_terms_that_caused_this_are_gone(banned):
    assert banned not in _queries()


def test_the_catalogue_really_is_build_work_only():
    """Kept as a tripwire on the catalogue itself: if it ever starts selling video or SNS work,
    the prohibitions are what would need revisiting, and this fails first."""
    text = _catalogue_text()
    for absent in ("動画編集", "SNS運用", "撮影"):
        assert absent not in text


def test_a_query_is_a_noun_phrase_not_a_title():
    """The recipe: 業務自動化システムを開発 finds nothing, 業務自動化 returns a live board."""
    for query in _queries():
        assert "します" not in query and "承ります" not in query
        assert len(query) <= 12
