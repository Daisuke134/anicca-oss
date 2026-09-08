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


# --- one vocabulary, three platforms -------------------------------------------------------

def test_discovery_terms_keeps_the_terms_measured_to_return_live_boards():
    """The original twelve earned their place by working, not by matching a catalogue row:
    「業務自動化システム」 is the catalogue title and 「業務自動化」 is what finds jobs."""
    terms = fit.discovery_terms()
    for proven in fit.PROVEN_BOARD_TERMS:
        assert proven in terms


def test_discovery_terms_folds_in_the_catalogue_without_duplicating():
    terms = fit.discovery_terms(("業務システム", "Shopify", "業務システム"))
    assert terms.count("業務システム") == 1
    assert "Shopify" in terms


def test_discovery_terms_drops_anything_naming_work_the_fleet_refuses():
    """Fetching refused work only manufactures skips -- the SNS運用 fallback, one layer up."""
    terms = fit.discovery_terms(("動画編集", "出品代行", "テレアポ", "業務システム"))
    assert "動画編集" not in terms and "出品代行" not in terms and "テレアポ" not in terms
    assert "業務システム" in terms


def test_the_non_catalogue_work_is_present_and_is_not_prohibited():
    terms = fit.discovery_terms()
    for expected in ("データ入力", "記事作成", "翻訳", "WordPress"):
        assert expected in terms
    for term in terms:
        assert fit.category_refusal(term) is None


# --- the artwork line, 2026-09-07 ------------------------------------------------------------

# Real Coconala category names, taken from its own 293-entry tree.
CRAFT = ("イラスト作成", "Vtuberイラスト・モデリング", "キャラクター作成・キャラデザ",
         "漫画制作・マンガ作成", "3Dアバター・衣装作成", "TRPGイラスト・立ち絵作成", "似顔絵作成")
WORKABLE_DESIGN = ("Webサイトデザイン", "HTML・CSSコーディング", "サムネイル作成・画像デザイン",
                   "AI生成画像の加工・レタッチ", "ロゴ作成・ロゴデザイン", "その他（デザイン制作）")


def test_producing_the_artwork_itself_is_refused():
    """Applied to 「YouTube・SNS用オリジナルキャラクター制作（Live2D＋情報発信用素材一式）」 at
    ¥250,000 on 2026-09-07. A rig is craft made in specialist tools over many passes, and taking
    one the fleet cannot finish costs a review rather than a proposal."""
    for label in CRAFT:
        assert fit.category_refusal(label) == ("original_illustration_or_modelling",
                                               fit.category_refusal(label)[1]), label


def test_design_that_produces_a_page_or_a_document_still_passes():
    """The line is what has to be produced, not the medium. Closing 'design' would have thrown
    away 「Webデザインのみ】採用サイトのデザイン制作」 at ¥300,000, taken the same day."""
    for label in WORKABLE_DESIGN:
        assert fit.category_refusal(label) is None, label


def test_the_class_is_in_the_prompt_the_judge_reads():
    assert "original_illustration_or_modelling" in fit.build_judgement_prompt(
        [{"posting_id": "1", "title": "t", "body": "b"}])
    assert "Live2D" in fit.HARD_PROHIBITION_CLASSES["original_illustration_or_modelling"]


def test_the_discovery_vocabulary_does_not_fetch_artwork_either():
    """Searching for work we now decline would only manufacture skips."""
    for term in fit.discovery_terms(("イラスト作成", "Live2Dモデリング", "業務システム")):
        assert fit.category_refusal(term) is None


# --- building is not operating, 2026-09-07 ---------------------------------------------------

def test_the_desktop_class_says_building_is_never_it():
    """Promoted from Coconala the same afternoon, this class then refused, on Lancers:

        RPAツール「アシロボ」シナリオ作成          -- building automation, the catalogue's core
        Notesからサイボウズ Officeへの移行とアプリ開発 -- a migration plus an app
        仮想通貨・Web3ライター（WordPress直接入稿）  -- an article, published through a tool

    22 refusals, roughly half of them work the fleet sells. A class promoted into a lane with a
    different catalogue has to be re-read against that catalogue, not assumed to transfer."""
    text = fit.HARD_PROHIBITION_CLASSES["mandatory_desktop_or_browser_operations"]
    assert "Building" in text and "migrating" in text
    assert "is a delivery and is never this class" in text


def test_the_class_still_names_what_it_is_for():
    """Sharpening must not empty it: operating an account for hours is still refused."""
    text = fit.HARD_PROHIBITION_CLASSES["mandatory_desktop_or_browser_operations"]
    for phrase in ("data entry", "monitoring", "repeated logged-in", "deliverable"):
        assert phrase in text, phrase


def test_the_prompt_carries_the_sharpened_wording():
    prompt = fit.build_judgement_prompt([{"posting_id": "1", "title": "t", "body": "b"}])
    assert "is a delivery and is never this class" in prompt


# --- the subject matter is not the deliverable, 2026-09-08 -----------------------------------

def test_writing_about_video_is_not_producing_video():
    """Dais found 「【動画ブランディング相談】初心者に寄り添い、ニッチな事業の魅力を一緒に整理して
    くださる方募集」 sitting unapplied-to. Advice is a document."""
    text = fit.HARD_PROHIBITION_CLASSES["video_or_animation"]
    assert "producing the footage itself" in text
    assert "is never this class" in text
    for allowed in ("Advice", "strategy", "scripts", "subtitles"):
        assert allowed in text, allowed


def test_writing_about_music_is_not_producing_music():
    """And 「音楽歌詞の多言語翻訳（ヒンディー語）」. Translating lyrics produces text."""
    text = fit.HARD_PROHIBITION_CLASSES["music_or_audio_production"]
    assert "producing the audio itself" in text
    assert "Lyrics, translation, transcription" in text
    assert "is never this class" in text


def test_the_classes_still_refuse_the_production_they_were_written_for():
    """Three classes now carry an 'is never this class' clause. None of them may be emptied by it."""
    for name, must_keep in (
        ("video_or_animation", ("video editing", "live-action filming", "animation")),
        ("music_or_audio_production", ("music", "composition", "mixing", "mastering")),
        ("mandatory_desktop_or_browser_operations", ("data entry", "monitoring")),
    ):
        text = fit.HARD_PROHIBITION_CLASSES[name]
        assert "is never this class" in text, name
        for phrase in must_keep:
            assert phrase in text, f"{name}: {phrase}"


def test_the_judge_prompt_carries_both_halves_of_each_line():
    prompt = fit.build_judgement_prompt([{"posting_id": "1", "title": "t", "body": "b"}])
    assert "producing the footage itself" in prompt
    assert "producing the audio itself" in prompt
