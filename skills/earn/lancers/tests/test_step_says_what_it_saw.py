"""_step() used to say only "form_changed" -- discarding the label it was matching, how many
things matched, and what page it was looking at. That is exactly the fault
skills/loop-engineering/references/marketplace-apply-lane.md's "refuse loudly" section names for
this lane specifically: `proposal_form_changed`, raised from 41 places on the sibling proposal
form, cost 81 skips in one day against 7 real declines because none of them named a selector.

This tests _step()'s new refusal path -- routed through
skills/_shared/marketplace-core/scripts/dom_contract.py's `exactly_one` -- without touching what
counts as a match: still exactly one *visible* element among every match for the label, still a
generic-looking `form_changed` for every one of _apply()'s five existing call sites (context is
opt-in), and still no click on anything but a clean single match.

Run: python3 -m pytest skills/earn/lancers/tests/test_step_says_what_it_saw.py
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "skills/earn/lancers/scripts/storefront_offer.py"
DOM_CONTRACT = REPO_ROOT / "skills/_shared/marketplace-core/scripts/dom_contract.py"


def _module():
    spec = importlib.util.spec_from_file_location("storefront_offer_step_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dom_contract():
    spec = importlib.util.spec_from_file_location("dom_contract_for_step_test", DOM_CONTRACT)
    dom = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dom
    spec.loader.exec_module(dom)
    return dom


class _Node:
    """One element get_by_text() could resolve to: its own visibility and a click counter."""

    def __init__(self, visible: bool = True) -> None:
        self.visible = visible
        self.clicks = 0

    def is_visible(self) -> bool:
        return self.visible

    def click(self, **_kwargs) -> None:
        self.clicks += 1


class _TextLocator:
    def __init__(self, nodes: list[_Node]) -> None:
        self._nodes = nodes

    def all(self) -> list[_Node]:
        return self._nodes


class _Page:
    """A minimal stand-in for the Playwright page _step() drives: get_by_text() resolves a
    label to whatever nodes the test wired up; url/title are what _page_identity() reads."""

    def __init__(self, matches: dict[str, list[_Node]], *, url: str, title: str | None = "パッケージ作成 | Lancers") -> None:
        self._matches = matches
        self.url = url
        self._title = title

    def get_by_text(self, label: str, exact: bool = True) -> _TextLocator:
        assert exact is True  # _step() has always required exact text matching
        return _TextLocator(self._matches.get(label, []))

    def title(self) -> str | None:
        return self._title


def _evidence_dir(module, tmp_path: Path) -> Path:
    directory = tmp_path / "evidence"
    module._EVIDENCE_DIR = directory
    return directory


# 1. Zero visible matches raises with the label and count 0 in the evidence ---------------------


def test_zero_visible_matches_names_the_label_and_count_in_the_evidence(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    page = _Page({"料金表": []}, url="https://www.lancers.jp/myplan/12345/edit")

    with pytest.raises(module.OfferError) as excinfo:
        module._step(page, "料金表")

    assert "label='料金表'" in str(excinfo.value)
    assert "found=0" in str(excinfo.value)
    dom = _dom_contract()
    rows = dom.failures(directory)
    assert len(rows) == 1
    assert rows[0]["selector"] == "料金表"
    assert rows[0]["found"] == 0
    assert rows[0]["platform"] == "lancers"


# 2. Two visible matches raises with count 2, and the strictness is unchanged -- it does not click


def test_two_visible_matches_names_the_count_and_never_clicks(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    first, second = _Node(visible=True), _Node(visible=True)
    page = _Page({"確認事項": [first, second]}, url="https://www.lancers.jp/myplan/12345/edit")

    with pytest.raises(module.OfferError) as excinfo:
        module._step(page, "確認事項")

    assert "found=2" in str(excinfo.value)
    assert first.clicks == 0
    assert second.clicks == 0
    dom = _dom_contract()
    assert dom.failures(directory)[0]["found"] == 2


def test_hidden_matches_do_not_count_towards_the_visible_total(tmp_path):
    """A wizard step's own label sits in the DOM at once whether or not that step is current
    (see the module comment above _CREATE_STEP_FIELDS) -- a hidden duplicate must never be
    confused for a second live match."""
    module = _module()
    _evidence_dir(module, tmp_path)
    visible, hidden = _Node(visible=True), _Node(visible=False)
    page = _Page({"業務内容": [hidden, visible]}, url="https://www.lancers.jp/myplan/add?type=manual")

    module._step(page, "業務内容")

    assert visible.clicks == 1
    assert hidden.clicks == 0


# 3. The evidence carries a page identity, not only the selector --------------------------------


def test_evidence_carries_page_identity_not_only_the_selector(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    page = _Page({"画像ほか": []}, url="https://www.lancers.jp/login", title="ログイン | Lancers")

    with pytest.raises(module.OfferError):
        module._step(page, "画像ほか")

    dom = _dom_contract()
    observed = dom.failures(directory)[0]["observed"]
    assert observed["url"] == "https://www.lancers.jp/login"
    assert observed["title"] == "ログイン | Lancers"


def test_a_page_that_cannot_answer_title_still_reports_its_url(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)

    class _BrokenTitlePage(_Page):
        def title(self):
            raise RuntimeError("page navigating")

    page = _BrokenTitlePage({"保存": []}, url="https://www.lancers.jp/myplan/12345/edit")

    with pytest.raises(module.OfferError):
        module._step(page, "保存")

    dom = _dom_contract()
    observed = dom.failures(directory)[0]["observed"]
    assert observed["url"] == "https://www.lancers.jp/myplan/12345/edit"
    assert observed["title"] is None


# Prove test 1 is not vacuous: reverting _step() to the bare "form_changed" the task describes
# must make this fail, and it does -- see the task's own verification instruction. Encoded here
# as a source check so a future edit that quietly drops the label/count back out is caught by
# the suite itself, not only by a one-time manual revert.
def test_step_source_no_longer_raises_a_bare_form_changed():
    source = SCRIPT.read_text(encoding="utf-8")
    step_source = source[source.index("def _step("):source.index("def _apply(")]
    assert 'raise OfferError("form_changed")' not in step_source
    assert "found={error.found}" in step_source


# 4. The chooser failure and the advance failure are distinguishable from each other -------------


def test_chooser_context_names_itself_distinctly_from_a_bare_step_failure(tmp_path):
    module = _module()
    _evidence_dir(module, tmp_path)
    page = _Page({module._CREATE_MANUAL_BUTTON_TEXT: []}, url=module.ORIGIN + "/myplan/add")

    with pytest.raises(module.OfferError) as excinfo:
        module._step(page, module._CREATE_MANUAL_BUTTON_TEXT, context="create_manual_chooser")

    message = str(excinfo.value)
    assert "create_manual_chooser" in message
    # _click_create_next_button's own stall path (the *other* half of create_package(), advancing
    # past a step rather than choosing the manual option) raises a differently-shaped error that
    # never runs through _step() at all -- so the two failures cannot collide into one code even
    # by coincidence.
    assert "create_step_stalled" not in message


def test_the_manual_chooser_call_site_passes_its_own_context():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '_step(page, _CREATE_MANUAL_BUTTON_TEXT, context="create_manual_chooser")' in source


def test_advance_failures_never_go_through_step_at_all():
    """_click_create_next_button (the wizard's 次へ) resolves its own click target independently
    of _step() -- see _resolve_create_advance_control's docstring, which says so explicitly. A
    chooser failure and an advance failure are distinguishable in the error precisely because
    they are raised by two different functions with two different prefixes: form_changed(:
    <context>) from _step(), create_step_stalled: <step> from _advance_create_step()."""
    source = SCRIPT.read_text(encoding="utf-8")
    click_next_source = source[source.index("def _click_create_next_button("):source.index("def _advance_create_step(")]
    assert "_step(" not in click_next_source
    assert "create_step_stalled" in click_next_source


# 5. _apply()'s behaviour is unchanged: still clicks the single visible match, still fails closed


def test_apply_call_sites_are_unmodified_bare_step_calls():
    """Every _step() call inside _apply() itself must still be the same two-positional-argument
    shape it always was -- no context, so its default (bare "form_changed"-shaped) behaviour is
    exactly what it was before this change."""
    source = SCRIPT.read_text(encoding="utf-8")
    apply_source = source[source.index("def _apply("):source.index("# --- Package creation")]
    step_calls = re.findall(r"_step\(page, [^)]*\)", apply_source)
    assert step_calls == ['_step(page, "保存")', '_step(page, "料金表")', '_step(page, "業務内容")',
                          '_step(page, "確認事項")', '_step(page, "画像ほか")']


def test_apply_style_call_with_no_context_still_fails_closed_on_ambiguity(tmp_path):
    """Exercises _step() exactly as _apply()'s own `_step(page, "保存")` call does: two visible
    "保存" nodes on the edit-form page (a real shape -- Lancers' edit form and its own confirm
    dialog can both carry a "保存" button) must still refuse rather than guess which one to
    click, with no context and the same bare "form_changed" prefix _apply() has always raised."""
    module = _module()
    _evidence_dir(module, tmp_path)
    first, second = _Node(visible=True), _Node(visible=True)
    page = _Page({"保存": [first, second]}, url="https://www.lancers.jp/myplan/12345/edit")

    with pytest.raises(module.OfferError) as excinfo:
        module._step(page, "保存")

    assert str(excinfo.value).startswith("form_changed: label='保存'")
    assert first.clicks == 0 and second.clicks == 0


# 6. A successful match still clicks exactly once ------------------------------------------------


def test_a_clean_single_match_still_clicks_exactly_once(tmp_path):
    module = _module()
    _evidence_dir(module, tmp_path)
    node = _Node(visible=True)
    page = _Page({"保存": [node]}, url="https://www.lancers.jp/myplan/12345/edit")

    module._step(page, "保存")

    assert node.clicks == 1


def test_a_clean_single_match_records_no_evidence(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    node = _Node(visible=True)
    page = _Page({"保存": [node]}, url="https://www.lancers.jp/myplan/12345/edit")

    module._step(page, "保存")

    dom = _dom_contract()
    assert dom.failures(directory) == []
