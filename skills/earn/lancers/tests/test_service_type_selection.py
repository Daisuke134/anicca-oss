"""_select_service_type() -- the one shared implementation of "select 業務
(ProjectPlanCategoryForm.service_type[0])" for both _apply() (edit an existing listing) and
_fill_create_form() (create_package()'s wizard).

Before this fix, create_package()'s own `_select_create_service_type` treated this control as a
`<select>` -- `select_option(label=...)` against a live `[name="ProjectPlanCategoryForm.
service_type[0]"]`. It never is one: the live DOM read behind this task's wake failure
(`form_changed: create:service_type: selector='[name="ProjectPlanCategoryForm.service_type[0]"]'
found=0`) showed it is a *radio group* -- many elements sharing that one `name`, each identified
by its own grandparent element's innerText, mounted only once subcategory is chosen (state
"attached", not "visible" at page load). `_apply()` already selected this control correctly,
live, in production; this task extracted that already-proven logic into `_select_service_type()`
and pointed both callers at it, so there is exactly one implementation left in the file.

These tests drive `_select_service_type()` directly against a small hand-built fake page --
never a real browser, never the live account -- covering: the attach-before-read wait, matching
by grandparent text (not by position or by "first available"), the three ways a match can be
rejected (zero, more than one, non-numeric value), the click-then-await-category-API sequence,
the two ways that round-trip can fail (non-200, never actually checked), and that both call sites
now go through this one function.

Run: python3 -m pytest skills/earn/lancers/tests/test_service_type_selection.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "skills/earn/lancers/scripts/storefront_offer.py"


def _module():
    spec = importlib.util.spec_from_file_location("storefront_offer_service_type_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- fakes --------------------------------------------------------------------------------------


class _Radio:
    """One radio in the group. `grandparent_text` models
    `e.parentElement.parentElement.innerText`; `value` models the radio's own `value` attribute,
    the id `label[for=<value>]` is keyed on in production. `checked` only ever flips true through
    `_Page._click_label` -- never merely because `_select_service_type` happened to match it."""

    def __init__(self, *, grandparent_text: str, value: str):
        self.grandparent_text = grandparent_text
        self.value = value
        self.checked = False

    def evaluate(self, _script: str) -> str:
        return self.grandparent_text

    def get_attribute(self, name: str) -> str | None:
        return self.value if name == "value" else None

    def is_checked(self) -> bool:
        return self.checked


class _RadioGroup:
    def __init__(self, radios: list[_Radio]):
        self._radios = radios

    def all(self) -> list[_Radio]:
        return list(self._radios)


class _Label:
    def __init__(self, page: "_Page", radio: _Radio | None):
        self._page = page
        self._radio = radio

    def click(self, **_kwargs) -> None:
        self._page._click_label(self._radio)


class _Response:
    def __init__(self, url: str, status: int):
        self.url = url
        self.status = status


class _ExpectResponse:
    def __init__(self, page: "_Page"):
        self._page = page
        self.value: _Response | None = None

    def __enter__(self) -> "_ExpectResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        self.value = self._page._last_response
        return False


class _Page:
    """A minimal stand-in for the Playwright page `_select_service_type` drives -- only the
    surface that function actually touches: `wait_for_selector`, `locator` (for the radio group
    and for `label[for=...]`), and `expect_response` around a label click.

    `attaches`: whether `wait_for_selector(..., state="attached")` succeeds -- False models the
    control never mounting at all. `api_status`: what the category lookup the label click
    triggers responds with. `click_checks`: whether that click actually ends up marking the radio
    checked -- False models a click that reached the server (`api_status=200`) but the UI never
    reflected it, exactly the fourth failure mode `_select_service_type`'s docstring names.
    """

    def __init__(self, *, radios: list[_Radio], attaches: bool = True, api_status: int = 200, click_checks: bool = True):
        self.calls: list[tuple] = []
        self._radios = radios
        self._attaches = attaches
        self._api_status = api_status
        self._click_checks = click_checks
        self._last_response: _Response | None = None

    def wait_for_selector(self, selector: str, *, state: str | None = None, timeout=None) -> None:
        self.calls.append(("wait_for_selector", selector, state))
        if not self._attaches:
            raise TimeoutError("service_type radio group never attached")

    def locator(self, selector: str):
        self.calls.append(("locator", selector))
        if selector.startswith('label[for="') and selector.endswith('"]'):
            value = selector[len('label[for="'):-2]
            radio = next((r for r in self._radios if r.value == value), None)
            return _Label(self, radio)
        return _RadioGroup(self._radios)

    def expect_response(self, predicate, timeout=None) -> _ExpectResponse:
        self.calls.append(("expect_response",))
        return _ExpectResponse(self)

    def _click_label(self, radio: _Radio | None) -> None:
        self.calls.append(("click_label", radio.value if radio is not None else None))
        if radio is None:
            self._last_response = None
            return
        self._last_response = _Response(
            f"https://www.lancers.jp/v1/project_store_api/project_category/{radio.value}", self._api_status,
        )
        if self._api_status == 200 and self._click_checks:
            radio.checked = True


_LABEL = "Webアプリケーション構築"


# 1. The control is waited for as attached before it is ever read --------------------------------


def test_waits_for_the_control_to_attach_before_reading_it():
    module = _module()
    page = _Page(radios=[_Radio(grandparent_text=_LABEL, value="42")])

    module._select_service_type(page, _LABEL)

    wait_index = next(i for i, call in enumerate(page.calls) if call[0] == "wait_for_selector")
    locator_index = next(i for i, call in enumerate(page.calls) if call[0] == "locator" and call[1] == module._SERVICE_TYPE_SELECTOR)
    assert page.calls[wait_index][1:] == (module._SERVICE_TYPE_SELECTOR, "attached")
    assert wait_index < locator_index

    page_never_attaches = _Page(radios=[_Radio(grandparent_text=_LABEL, value="42")], attaches=False)
    with pytest.raises(TimeoutError):
        module._select_service_type(page_never_attaches, _LABEL)
    # Never fell through to reading radios once the wait itself failed.
    assert not any(call[0] == "click_label" for call in page_never_attaches.calls)


# 2. Selection is by the radio's own grandparent text, not by position or "first available" ------
#
# Proven non-vacuous manually while writing this: temporarily changing _select_service_type to
# pick radios[0] regardless of label made this test fail (it picked the decoy, listed first);
# reverting made it pass again.


def test_selects_the_radio_whose_grandparent_text_equals_the_requested_label():
    module = _module()
    decoy = _Radio(grandparent_text="デコイ業務", value="1")
    target = _Radio(grandparent_text=_LABEL, value="42")
    page = _Page(radios=[decoy, target])

    module._select_service_type(page, _LABEL)

    assert target.checked is True
    assert decoy.checked is False


# 3. Zero matches raises, naming the label and the count ------------------------------------------


def test_zero_matches_raises_naming_the_label_and_the_count():
    module = _module()
    page = _Page(radios=[_Radio(grandparent_text="別の業務", value="1")])

    with pytest.raises(module.OfferError) as excinfo:
        module._select_service_type(page, _LABEL, context="create:service_type")

    message = str(excinfo.value)
    assert "create:service_type" in message
    assert repr(_LABEL) in message
    assert "found=0" in message
    assert "別の業務" in message  # what was actually seen is named, not discarded


# 4. More than one match raises, naming the count ------------------------------------------------


def test_more_than_one_match_raises_naming_the_count():
    module = _module()
    page = _Page(radios=[
        _Radio(grandparent_text=_LABEL, value="1"),
        _Radio(grandparent_text=_LABEL, value="2"),
    ])

    with pytest.raises(module.OfferError) as excinfo:
        module._select_service_type(page, _LABEL)

    message = str(excinfo.value)
    assert "found=2" in message
    assert not any(radio.checked for radio in page._radios)  # an ambiguous match is never clicked


# 5. A non-numeric value raises, named ------------------------------------------------------------


def test_non_numeric_radio_value_raises_named():
    module = _module()
    page = _Page(radios=[_Radio(grandparent_text=_LABEL, value="not-an-id")])

    with pytest.raises(module.OfferError) as excinfo:
        module._select_service_type(page, _LABEL)

    message = str(excinfo.value)
    assert repr(_LABEL) in message
    assert "not-an-id" in message
    assert not any(call[0] == "click_label" for call in page.calls)  # never clicked an unusable value


# 6. The click targets label[for=<value>] and the category API response is awaited ----------------


def test_click_targets_label_for_value_and_awaits_the_category_api_response():
    module = _module()
    radio = _Radio(grandparent_text=_LABEL, value="42")
    page = _Page(radios=[radio])

    module._select_service_type(page, _LABEL)

    assert ("locator", 'label[for="42"]') in page.calls
    assert ("expect_response",) in page.calls
    assert ("click_label", "42") in page.calls
    # The click happened only after expect_response had already started listening.
    expect_index = page.calls.index(("expect_response",))
    click_index = page.calls.index(("click_label", "42"))
    assert expect_index < click_index


# 7. A non-200 API response, or a radio that never actually ends up checked, raises named ---------


def test_non_200_api_response_raises_named():
    module = _module()
    radio = _Radio(grandparent_text=_LABEL, value="42")
    page = _Page(radios=[radio], api_status=500)

    with pytest.raises(module.OfferError) as excinfo:
        module._select_service_type(page, _LABEL)

    assert repr(_LABEL) in str(excinfo.value)
    assert radio.checked is False


def test_radio_left_unchecked_after_a_successful_response_still_raises_named():
    module = _module()
    radio = _Radio(grandparent_text=_LABEL, value="42")
    page = _Page(radios=[radio], api_status=200, click_checks=False)

    with pytest.raises(module.OfferError) as excinfo:
        module._select_service_type(page, _LABEL)

    assert repr(_LABEL) in str(excinfo.value)


# 8. Both _apply() and _fill_create_form() go through this one helper -- no second implementation -


def test_both_call_sites_use_the_one_shared_helper_and_no_second_implementation_exists():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "def _select_create_service_type(" not in source
    assert source.count("def _select_service_type(") == 1

    apply_source = source[source.index("def _apply("):source.index("# --- Package creation")]
    assert '_select_service_type(page, product["service_type"])' in apply_source

    fill_source = source[source.index("def _fill_create_form("):source.index("def _create_submit_control(")]
    assert '_select_service_type(page, product["service_type"], context="create:service_type")' in fill_source


# 9. _apply()'s call site is unchanged: still bare (no context), exactly like every other _field()/
#    _step() call inside _apply() -- see those functions' own docstrings for the convention -------


def test_apply_still_calls_the_helper_bare_with_no_context_argument():
    source = SCRIPT.read_text(encoding="utf-8")
    apply_source = source[source.index("def _apply("):source.index("# --- Package creation")]
    assert '_select_service_type(page, product["service_type"])\n' in apply_source
    assert '_select_service_type(page, product["service_type"], context=' not in apply_source
