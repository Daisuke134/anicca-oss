"""Pinned to the category labels CrowdWorks actually printed on 2026-09-07, not to guesses.

The allow-list this replaces was itself introduced as "measured, not guessed", was widened once
for the categories it had wrongly refused, and was still refusing 59 of 98 open postings a week
later -- including one of the very categories it had been widened for. So the test that matters
is not "does the list contain the right words" but "of the labels the marketplace really emitted,
does exactly the unworkable ones get refused".

Run: python3 -m pytest skills/_shared/marketplace-core/tests/test_work_fit.py
"""

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("work_fit_under_test", SCRIPTS / "work_fit.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fit = _load()

# Measured from the lane's own decline messages, 2026-09-07.
REFUSED_BY_THE_OLD_ALLOW_LIST = (
    "セールス・営業支援", "質問・アンケート", "動画作成・動画制作", "データ入力",
    "広告・宣伝", "カスタマーサポート", "その他（デザイン）", "TikTok・ショート動画",
    "HTML・CSSコーディング", "AI・チャットボット開発",
)
UNWORKABLE = {"動画作成・動画制作", "TikTok・ショート動画"}


def test_only_the_unworkable_measured_categories_are_refused():
    refused = {label for label in REFUSED_BY_THE_OLD_ALLOW_LIST if fit.category_refusal(label)}
    assert refused == UNWORKABLE


def test_the_two_categories_the_catalogue_sells_are_no_longer_refused():
    """Both were named in the old list's own comment as things it had wrongly rejected."""
    assert fit.category_refusal("HTML・CSSコーディング") is None
    assert fit.category_refusal("AI・チャットボット開発") is None


def test_a_refusal_names_the_class_and_the_word_that_triggered_it():
    assert fit.category_refusal("動画作成・動画制作") == ("video_or_animation", "動画")


def test_an_unknown_category_is_workable():
    """The asymmetry that made the allow-list expensive: refusing an unknown label costs every
    posting under it, silently, while bidding on one costs one proposal."""
    assert fit.category_refusal("まだ存在しないカテゴリ") is None
    assert fit.category_refusal("") is None


def test_development_words_that_merely_contain_a_banned_substring_still_pass():
    """音声 and 撮影 are left out of the term list precisely because they sit inside work we want."""
    assert fit.category_refusal("音声認識AI開発") is None
    assert fit.category_refusal("撮影スタジオ予約システム開発") is None


def test_the_known_limit_of_judging_by_label_alone():
    """A label containing 動画 is refused even when the work is a build. This is accepted, not
    overlooked: CrowdWorks emits a fixed set of category names and none of them is of this shape,
    and an adapter that has the posting text should ask an LLM against HARD_PROHIBITION_CLASSES
    instead of calling this function."""
    assert fit.category_refusal("動画配信システム開発") == ("video_or_animation", "動画")


def test_the_prohibitions_that_predate_this_module_are_carried_over():
    for key in ("video_or_animation", "physical_or_onsite", "mandatory_human_presence",
                "manual_marketplace_operation", "mandatory_attribute_fabrication",
                "missing_legal_qualification", "illegal_or_unsafe"):
        assert key in fit.HARD_PROHIBITION_CLASSES
    # Dais 2026-09-07.
    assert "explicit_ai_prohibition" not in fit.HARD_PROHIBITION_CLASSES


def test_every_category_term_belongs_to_a_declared_prohibition_class():
    for prohibition, terms in fit.PROHIBITED_CATEGORY_TERMS:
        assert prohibition in fit.HARD_PROHIBITION_CLASSES
        assert terms
