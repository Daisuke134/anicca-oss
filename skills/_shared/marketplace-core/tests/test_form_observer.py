"""Pin the platform-neutral marketplace listing-form observer.

Fixtures under fixtures/form_observer/ are hand-written static HTML, modeling the shapes a real
marketplace listing-creation form is documented (SKILL context, 2026-09-07) to actually take: a
select whose placeholder must be told apart from its real choices, an unnamed textarea, a
six-step wizard whose non-current steps sit in the DOM wrapped in a CSS-module class that only a
structural signature -- never a hardcoded string -- should be able to find, and a plain
single-page form that must not be mistaken for one.

Run: python3 -m pytest skills/_shared/marketplace-core/tests/test_form_observer.py
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "form_observer.py"
SPEC = importlib.util.spec_from_file_location("marketplace_form_observer", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
observer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = observer
SPEC.loader.exec_module(observer)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "form_observer"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _field(report: dict, identifier: str) -> dict:
    match = next(f for f in report["fields"] if f["identifier"] == identifier)
    return match


# --- 1. select options reported in order, placeholder identified --------------------------

def test_select_options_are_reported_in_order_with_placeholder_identified():
    report = observer.observe_html(_html("single_select_form.html"))
    category = _field(report, "category")
    assert category["tag"] == "select"
    assert category["option_labels"] == [
        "選択してください", "Web制作・システム開発", "ライティング・翻訳", "デザイン",
    ]
    assert category["placeholder_index"] == 0
    assert [option["is_placeholder"] for option in category["options"]] == [True, False, False, False]


# --- 2. an unnamed textarea is reported and distinguishable from a named one ---------------

def test_unnamed_textarea_is_reported_and_distinguishable():
    report = observer.observe_html(_html("single_select_form.html"))
    textareas = [f for f in report["fields"] if f["tag"] == "textarea"]
    assert len(textareas) == 1
    unnamed = textareas[0]
    assert unnamed["identifier"] is None
    assert unnamed["identifier_source"] == "none"
    assert unnamed["required"] is True

    named_title = _field(report, "title")
    assert named_title["identifier_source"] == "name"


# --- 3. wizard detected from a repeated hiding wrapper; step->field map; class reported ----

def test_wizard_is_detected_and_steps_map_to_the_right_fields():
    report = observer.observe_html(_html("wizard.html"))
    steps = report["steps"]
    assert steps["is_wizard"] is True
    assert steps["wrapper_class"] == "_hidden_p1vuy_39"
    assert steps["detected_via"]["sibling_count"] == 6
    assert steps["detected_via"]["children_with_wrapper_class"] == 5
    assert len(steps["steps"]) == 6

    step2 = steps["steps"][1]
    assert step2["has_wrapper_class"] is True
    assert set(step2["fields"]) == {"title", "description"}

    step1 = steps["steps"][0]
    assert step1["has_wrapper_class"] is False
    assert set(step1["fields"]) == {"category", "subcategory"}

    step6 = steps["steps"][5]
    assert step6["fields"] == ["agree_terms"]


# --- 4. a non-wizard single-page form reports no steps rather than inventing one -----------

def test_single_page_form_reports_no_steps():
    report = observer.observe_html(_html("single_page_form.html"))
    assert report["steps"] == {
        "is_wizard": False, "wrapper_class": None, "detected_via": None, "steps": [],
    }


# --- 5. a different build hash still produces the same detection: nothing is hardcoded -----

def test_a_different_build_hash_is_detected_the_same_way():
    report = observer.observe_html(_html("wizard_diff_hash.html"))
    steps = report["steps"]
    assert steps["is_wizard"] is True
    assert steps["wrapper_class"] == "_hidden_x7q2m_81"
    assert len(steps["steps"]) == 6
    assert set(steps["steps"][1]["fields"]) == {"title", "description"}


# --- 6. a select carrying only a placeholder is a suspected dependent, with evidence -------

def test_placeholder_only_select_is_a_suspected_dependent():
    report = observer.observe_html(_html("wizard.html"))
    dependents = {entry["identifier"]: entry for entry in report["dependent_selects"]}
    assert "subcategory" in dependents
    evidence = dependents["subcategory"]["evidence"]
    assert evidence["option_count"] == 1
    assert evidence["placeholder_label"] == "選択してください"


# --- 7. a select with real options is not reported as a dependent --------------------------

def test_select_with_real_options_is_not_a_dependent():
    report = observer.observe_html(_html("wizard.html"))
    dependent_ids = {entry["identifier"] for entry in report["dependent_selects"]}
    assert "category" not in dependent_ids
    assert "delivery_days" not in dependent_ids


# --- 8. diff_vocabulary finds a claimed value absent from the real options -----------------

def test_diff_vocabulary_finds_an_absent_claim_and_names_the_closest_real_option():
    observed = ["Web制作・システム開発", "ライティング・翻訳", "デザイン"]
    result = observer.diff_vocabulary(observed, {"my_family": "システム開発"})
    assert "my_family" in result["absent"]
    assert result["absent"]["my_family"]["claimed"] == "システム開発"
    assert result["absent"]["my_family"]["closest"] == "Web制作・システム開発"
    assert result["present"] == {}


# --- 9. diff_vocabulary on an exact match reports it present and flags nothing -------------

def test_diff_vocabulary_reports_an_exact_match_as_present():
    observed = ["Web制作・システム開発", "ライティング・翻訳", "デザイン"]
    result = observer.diff_vocabulary(observed, {"my_family": "デザイン"})
    assert result["present"] == {"my_family": "デザイン"}
    assert result["absent"] == {}


# --- 10. the module names no marketplace, same style storefront_kernel.py asserts of itself -

def test_module_contains_no_platform_name():
    source = MODULE_PATH.read_text(encoding="utf-8")
    for platform in ("lancers", "coconala", "crowdworks"):
        assert platform not in source.lower()


# --- 11. observe_html omits live-only fields rather than guessing them ---------------------

def test_observe_html_omits_live_only_fields():
    report = observer.observe_html(_html("single_select_form.html"))
    for field in report["fields"]:
        assert "visible" not in field
        assert "bounding_box" not in field
        assert "hiding_ancestor" not in field


# --- extra: advance_control reports every candidate and the chosen one ---------------------

def test_advance_control_reports_candidates_and_the_chosen_next_button():
    report = observer.observe_html(_html("wizard.html"))
    advance = report["advance_control"]
    texts = {c["text"] for c in advance["candidates"]}
    assert texts == {"次へ", "出品する"}
    assert advance["chosen"]["text"] == "次へ"


def test_advance_control_none_when_nothing_matches_the_keyword_list():
    report = observer.observe_html(_html("single_page_form.html"))
    advance = report["advance_control"]
    assert advance["chosen"] is None
    assert advance["candidates"] == [{
        "tag": "button", "text": "送信する", "identifier": None, "identifier_source": "none",
    }]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
