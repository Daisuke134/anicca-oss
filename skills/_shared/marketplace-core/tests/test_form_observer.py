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


# --- step_requirements: a minimal live-page fake ---------------------------------------------
#
# observe_page needs a live Locator surface: .content(), .locator(selector).is_visible(),
# .bounding_box(), .input_value(), and (for a <select>) .locator("option:checked"). These fakes
# resolve the same positional css_path selectors observe_page's own field-visibility check
# already issues (`_Node.css_path()`), by re-parsing the same static HTML observe_page itself
# parsed -- exactly the pattern skills/earn/lancers/tests/test_create_package.py's own
# _ObservablePage/_PositionalLocator use against this same module, just scoped small here since
# this file only needs it for step_requirements, not a whole wizard-fill simulation.

class _LiveOptionNode:
    def __init__(self, node):
        self._node = node

    def get_attribute(self, name: str) -> str | None:
        return self._node.attrs.get(name)

    def inner_text(self) -> str:
        return self._node.text


class _LiveOptionList:
    def __init__(self, nodes):
        self._nodes = nodes

    def count(self) -> int:
        return len(self._nodes)

    def all(self):
        return [_LiveOptionNode(node) for node in self._nodes]


class _LiveLocator:
    """Resolves visibility (any ancestor with display:none/visibility:hidden), a text control's
    value (an <input>'s value attribute, or a <textarea>'s own text), and a <select>'s checked
    option (the one carrying `selected`, defaulting to the first -- a real unset <select> always
    has exactly one option checked, its first, mirroring every other native-select fake in this
    house)."""

    def __init__(self, node):
        self._node = node

    def _hidden(self) -> bool:
        node = self._node
        while node is not None and node.tag != "#root":
            style = (node.attrs.get("style") or "").replace(" ", "")
            if "display:none" in style or "visibility:hidden" in style:
                return True
            node = node.parent
        return False

    def is_visible(self) -> bool:
        return not self._hidden()

    def bounding_box(self):
        return {"width": 0, "height": 0} if self._hidden() else {"width": 120, "height": 24}

    def input_value(self) -> str:
        if self._node.tag == "textarea":
            return self._node.text
        return self._node.attrs.get("value") or ""

    def locator(self, selector: str):
        if selector != "option:checked":
            raise NotImplementedError(selector)
        options = [child for child in self._node.children if child.tag == "option"]
        selected = [option for option in options if "selected" in option.attrs]
        chosen = selected[0] if selected else (options[0] if options else None)
        return _LiveOptionList([chosen] if chosen is not None else [])

    def evaluate(self, _script: str):
        raise NotImplementedError


class _MissingLiveLocator:
    def is_visible(self):
        return None

    def bounding_box(self):
        return None

    def input_value(self) -> str:
        return ""

    def locator(self, _selector: str):
        return _LiveOptionList([])


class _LivePage:
    def __init__(self, html: str):
        self._html = html
        self._root = observer._parse_tree(html)

    def content(self) -> str:
        return self._html

    def locator(self, selector: str):
        node = self._resolve(selector)
        return _LiveLocator(node) if node is not None else _MissingLiveLocator()

    def _resolve(self, selector: str):
        node = self._root
        for part in selector.split(" > "):
            tag, rest = part.split(":nth-of-type(")
            index = int(rest.rstrip(")")) - 1
            matches = [child for child in node.children if child.tag == tag]
            if index >= len(matches):
                return None
            node = matches[index]
        return node


def _requirements_report() -> dict:
    return observer.observe_page(_LivePage(_html("requirements.html")))


# --- step_requirements: label, required flag, and filled state for native controls -----------

def test_step_requirements_reports_label_required_flag_and_filled_state():
    by_label = {entry["label"]: entry for entry in _requirements_report()["step_requirements"]}
    assert by_label["タイトル"]["required"] is True
    assert by_label["タイトル"]["filled"] is True
    assert by_label["サブタイトル"]["required"] is False
    assert by_label["サブタイトル"]["filled"] is False


# --- step_requirements: the whole point -- a required control named nowhere else -------------

def test_step_requirements_reports_a_required_control_named_nowhere_else():
    """業種 is backed by a custom widget with no native input/select/textarea at all -- nothing
    in this test ever told the observer to expect it by name. It still appears, with its visible
    label, because this is built from the DOM outward, not from a caller's known field list."""
    labels = {entry["label"] for entry in _requirements_report()["step_requirements"]}
    assert "業種" in labels


# --- step_requirements: a required select holding only its placeholder reads empty -----------

def test_step_requirements_select_with_only_placeholder_reports_empty():
    by_label = {entry["label"]: entry for entry in _requirements_report()["step_requirements"]}
    assert by_label["カテゴリー"]["tag"] == "select"
    assert by_label["カテゴリー"]["filled"] is False


# --- step_requirements: a select whose checked option is a placeholder by LABEL, not value ---
# (the false negative this shipped from: value="0" is truthy, but 未選択 means nothing chosen)

def test_step_requirements_select_with_nonempty_value_placeholder_label_reports_empty():
    by_label = {entry["label"]: entry for entry in _requirements_report()["step_requirements"]}
    assert by_label["業務"]["tag"] == "select"
    assert by_label["業務"]["filled"] is False


def test_step_requirements_select_with_a_longer_placeholder_phrase_reports_empty():
    """業務を選択してください contains 選択してください, not an exact match -- containment, not
    equality, is what this must key off."""
    by_label = {entry["label"]: entry for entry in _requirements_report()["step_requirements"]}
    assert by_label["専門知識"]["filled"] is False


def test_step_requirements_select_with_a_real_option_checked_still_reports_filled():
    """A select is not blanket-downgraded to unfilled just because it has a placeholder option
    somewhere in its list -- only when the *checked* option is the placeholder."""
    by_label = {entry["label"]: entry for entry in _requirements_report()["step_requirements"]}
    assert by_label["技術"]["filled"] is True


# --- step_requirements: a custom widget reports present-but-unreadable, not filled -----------

def test_step_requirements_custom_widget_reports_unreadable_not_filled():
    by_label = {entry["label"]: entry for entry in _requirements_report()["step_requirements"]}
    assert by_label["業種"]["readable"] is False
    assert by_label["業種"]["filled"] is None


# --- step_requirements: only the visible step's controls are reported ------------------------

def test_step_requirements_excludes_a_required_control_on_a_hidden_step():
    labels = {entry["label"] for entry in _requirements_report()["step_requirements"]}
    assert "非表示項目" not in labels


# --- step_requirements: live-only, absent from observe_html rather than guessed --------------

def test_step_requirements_absent_from_observe_html():
    report = observer.observe_html(_html("requirements.html"))
    assert "step_requirements" not in report


# --- proof test_step_requirements_reports_a_required_control_named_nowhere_else is not vacuous -

def test_step_requirements_is_not_vacuous_when_restricted_to_known_names():
    """Per the task: restrict the enumeration to a fixed set of known names, confirm 業種 then
    fails to appear, revert, confirm it is reported again."""
    original = observer._step_requirement_candidates
    known = {"タイトル", "サブタイトル", "カテゴリー"}
    observer._step_requirement_candidates = lambda root: [
        candidate for candidate in original(root) if candidate["label"] in known
    ]
    try:
        restricted = {entry["label"] for entry in _requirements_report()["step_requirements"]}
        assert "業種" not in restricted
    finally:
        observer._step_requirement_candidates = original

    restored = {entry["label"] for entry in _requirements_report()["step_requirements"]}
    assert "業種" in restored


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
