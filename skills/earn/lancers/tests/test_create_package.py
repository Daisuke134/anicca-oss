"""create_package() -- the manual-creation counterpart to _apply().

_apply() can only edit a package that already carries a listing_external_id: it goes straight
to /myplan/{listing_id}/edit. There was no path from the shared catalogue (twenty families,
now all projecting to legal Lancers plans) to a brand-new Lancers package -- one hand-authored
listing was all this lane could ever produce. create_package() is that path, reached by
clicking the manual option at /myplan/add -> /myplan/add?type=manual.

A live DOM read (see the task this file's wizard-walking coverage shipped from) found that
target is not one flat form: it is a six-step wizard (基本情報 / 料金表 / 業務内容 / 確認事項 /
画像ほか / 公開). Every step's fields sit in the DOM at once; only the current step's fields have
a real bounding box. The fakes below model exactly that: each field carries the wizard step
index it belongs to, and is "visible" only while the fake page's `current_step` matches --
`_Field.fill()`/`.select_option()` self-check that invariant and raise if violated, so any test
that drives `_fill_create_form()` is automatically also a regression test against a flat,
non-wizard-aware fill order (confirmed manually while writing this: reverting
_fill_create_form to fill every field before any 次へ click makes
test_fields_are_never_filled_before_their_step_is_current fail with an AssertionError from
inside _Field.fill, then passes again once the step-walk is restored).

These tests exercise the module against small hand-built fakes (following the convention in
apps/lancers-revenue/tests/test_application_loop_hol.py and
skills/earn/crowdworks/tests/test_application_tick_uncertainty.py: plain Python objects that
record what was called and let a `wait_for_url`-style callback mutate `page.url` as its
production counterpart would), never a real browser and never the live account.

Run: python3 -m pytest skills/earn/lancers/tests/test_create_package.py
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "skills/earn/lancers/scripts/storefront_offer.py"
FORM_OBSERVER_SCRIPT = REPO_ROOT / "skills/_shared/marketplace-core/scripts/form_observer.py"


def _module():
    spec = importlib.util.spec_from_file_location("storefront_offer_create_package_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_form_observer():
    """The same platform-neutral observer storefront_offer._reach_form_observer() reaches for in
    production, loaded directly here only so _ObservablePage (below) can reuse its internal
    `_parse_tree`/`_Node` to resolve the exact positional locators observe_page issues -- this is
    test-harness plumbing, not a second implementation of step detection."""
    spec = importlib.util.spec_from_file_location("lancers_test_form_observer", FORM_OBSERVER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_OBSERVER = _load_form_observer()


# --- fakes ------------------------------------------------------------------------------------
#
# Wizard step indices (mirroring the live stepper's own order): 0=基本情報, 1=料金表,
# 2=業務内容, 3=確認事項, 4=画像ほか, 5=公開.

# The 16 delivery_time options the live DOM read carried: a blank placeholder plus one label per
# day count Lancers' project_lancers()/LANCERS_DELIVERY_DAYS actually offers.
_DELIVERY_DAYS = (1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 30, 45, 60, 75, 90)
_IMAGE_STEP_MARKER_TEXT = "受注率が約10倍になります"  # mirrors storefront_offer._CREATE_IMAGE_STEP_MARKER_TEXT
_SERVICE_TYPE_SELECTOR = '[name="ProjectPlanCategoryForm.service_type[0]"]'  # mirrors storefront_offer._SERVICE_TYPE_SELECTOR


class _Option:
    def __init__(self, label: str, value: str):
        self._label = label
        self._value = value

    def inner_text(self) -> str:
        return self._label

    def get_attribute(self, name: str) -> str | None:
        return self._value if name == "value" else None


class _OptionList:
    """`<select> option` enumeration only -- see _LocatorList for button/text-match lists."""

    def __init__(self, options: list[_Option]):
        self._options = options

    def all(self) -> list[_Option]:
        return list(self._options)

    def count(self) -> int:
        return len(self._options)


def _delivery_options() -> list[_Option]:
    options = [_Option("選択してください", "")]
    options.extend(_Option(f"{days}日", str(days)) for days in _DELIVERY_DAYS)
    return options


class _LocatorList:
    """A Playwright Locator that may resolve to zero or more elements -- what
    `page.locator("button")` / `page.get_by_text(...)` return in production. `.all()` mirrors
    Playwright's own eager per-element list; `.wait_for()` mirrors waiting for at least one
    visible match, which is all _advance_create_step ever needs it for.
    """

    def __init__(self, items: list):
        self._items = list(items)

    def all(self) -> list:
        return list(self._items)

    def count(self) -> int:
        return len(self._items)

    def inner_text(self) -> str:
        # Mirrors real Playwright strict-mode Locator.inner_text(): only sensible on a locator
        # resolving to exactly one element -- _create_describing_text's `page.locator(f"#{id}")`
        # call is exactly that shape.
        if len(self._items) != 1:
            raise AssertionError(f"strict mode violation: {len(self._items)} matches")
        return self._items[0].inner_text()

    def wait_for(self, state: str = "visible", timeout=None) -> None:
        if state != "visible":
            raise NotImplementedError(state)
        if not any(item.is_visible() for item in self._items):
            raise TimeoutError("no visible match")

    def click(self, **kwargs) -> None:
        # Mirrors real Playwright strict-mode Locator.click(): only sensible on a locator
        # resolving to exactly one element.
        if len(self._items) != 1:
            raise AssertionError(f"strict mode violation: {len(self._items)} matches")
        self._items[0].click(**kwargs)


class _Field:
    """One form control. Records every fill/select_option/press call it receives.

    `step`, when set, ties visibility to the owning `_FakeCreatePage.current_step` -- the same
    "hidden until this wizard step is current" behaviour the live DOM read described. `fill()`
    and `select_option()` assert `is_visible()` at call time and raise AssertionError otherwise:
    this is what makes every test driving _fill_create_form() double as a check that a field is
    never touched before its step actually arrived.

    `attrs` models arbitrary element attributes (aria-invalid, aria-describedby, aria-label,
    disabled, ...) read via `get_attribute()` -- the stall-evidence report reads these on real
    Playwright locators, so the fake needs to be able to carry them too. For a `<select>`
    (`options` non-empty), `_selected_index` mirrors the one real browsers keep even when nothing
    has ever been explicitly chosen -- it defaults to 0 (the first/placeholder option) exactly as
    an unset native `<select>` does, and `select_option()` updates it, so `option:checked` always
    reflects genuine selection state rather than merely "was select_option ever called".

    `outer_html`, when set, is what `.evaluate()` returns -- modelling `el => el.outerHTML`.
    """

    def __init__(self, *, options: list[_Option] | None = None, visible: bool = True, text: str = "", step: int | None = None, name: str = "", attrs: dict[str, str] | None = None, outer_html: str | None = None):
        self.fills: list[str] = []
        self.selected: list[dict] = []
        self.presses: list[str] = []
        self.clicks = 0
        self._options = options or []
        self._visible = visible
        self._text = text
        self._step = step
        self._name = name
        self._attrs = attrs or {}
        self._outer_html = outer_html
        self._selected_index: int | None = 0 if self._options else None
        self.page: "_FakeCreatePage | None" = None  # bound by _FakeCreatePage.__init__

    def count(self) -> int:
        return 1

    def is_visible(self) -> bool:
        if self._step is not None:
            return self.page is not None and self.page.current_step == self._step
        return self._visible

    def _log(self, action: str) -> None:
        if self.page is not None:
            self.page.event_log.append((action, self._name, self.page.current_step))

    def fill(self, value: str) -> None:
        if not self.is_visible():
            raise AssertionError(f"filled invisible field {self._name!r} (step={self._step}, current={getattr(self.page, 'current_step', None)})")
        self.fills.append(value)
        self._log("fill")

    def select_option(self, *_args, **kwargs) -> None:
        if not self.is_visible():
            raise AssertionError(f"selected option on invisible field {self._name!r} (step={self._step}, current={getattr(self.page, 'current_step', None)})")
        self.selected.append(kwargs)
        self._log("select_option")
        index = self._matching_option_index(kwargs)
        if index is not None:
            self._selected_index = index

    def _matching_option_index(self, kwargs: dict) -> int | None:
        label = kwargs.get("label")
        value = kwargs.get("value")
        for index, option in enumerate(self._options):
            if label is not None and option._label == label:
                return index
            if value is not None and option._value == value:
                return index
        return None

    def press(self, key: str) -> None:
        if not self.is_visible():
            raise AssertionError(f"pressed key on invisible field {self._name!r} (step={self._step})")
        self.presses.append(key)

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)

    def input_value(self) -> str:
        return self.fills[-1] if self.fills else ""

    def click(self, **_kwargs) -> None:
        self.clicks += 1

    def locator(self, selector: str):
        if selector == "option":
            return _OptionList(self._options)
        if selector == "option:checked":
            if self._options and self._selected_index is not None:
                return _OptionList([self._options[self._selected_index]])
            return _OptionList([])
        # "img" -- the clickable-control census's nested-icon lookup (see
        # form_observer.clickable_controls). No fixture in this file nests an <img> inside a
        # control, so this always reads as "no nested image", exactly like a real control with
        # no icon.
        return _OptionList([])

    def wait_for(self, state: str = "visible", timeout=None) -> None:
        if state != "visible":
            raise NotImplementedError(state)
        if not self.is_visible():
            raise TimeoutError(f"field {self._name!r} not visible (step={self._step})")

    def evaluate(self, _script: str) -> str:
        if self._outer_html is not None:
            return self._outer_html
        return f"<button>{self._text}</button>"


class _EmptyField:
    """Anything the fake page was never told about: count()==0, so _field()/_public() raise
    predictably instead of KeyError-ing -- the same shape a real, unmatched Playwright locator
    produces.
    """

    def count(self) -> int:
        return 0

    def get_attribute(self, _name: str) -> str | None:
        return None

    def all(self) -> list:
        return []

    def wait_for(self, state: str = "visible", timeout=None) -> None:
        raise TimeoutError("no such element")


class _UnnamedTextarea:
    """Models `textarea:not([name])` -- 業務内容's field. Present in the DOM at a configurable
    multiplicity (`count`) regardless of which step is current (the live DOM read carries every
    step's fields at once, so a structural duplicate/absence is not a visibility question), but
    only "visible" while step 2 (業務内容) is current, exactly like every other wizard field.
    """

    def __init__(self, page: "_FakeCreatePage", *, count: int = 1, step: int = 2):
        self.page = page
        self._count = count
        self._step = step
        self.fills: list[str] = []

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return self._count == 1 and self.page.current_step == self._step

    def fill(self, value: str) -> None:
        if not self.is_visible():
            raise AssertionError(f"filled invisible unnamed textarea (current={self.page.current_step})")
        self.fills.append(value)
        self.page.event_log.append(("fill", "業務内容", self.page.current_step))

    def wait_for(self, state: str = "visible", timeout=None) -> None:
        if state != "visible":
            raise NotImplementedError(state)
        if not self.is_visible():
            raise TimeoutError("unnamed textarea not visible")


class _FileInput:
    def __init__(self, *, raises: bool = False):
        self.set_files_calls: list[str] = []
        self._raises = raises

    def set_input_files(self, path: str) -> None:
        if self._raises:
            raise RuntimeError("upload failed")
        self.set_files_calls.append(path)


class _FileInputs:
    """Models `input[type="file"]` -- the four uploads on 画像ほか. Deliberately not
    step-visibility-gated: upload widgets commonly keep the native input present-but-styled-
    invisible even on their own step, which is exactly why _fill_create_form treats a file
    input's *count*, not its visibility, as the signal for whether an attach is attempted.
    """

    def __init__(self, count: int = 4, *, raises: bool = False):
        self._items = [_FileInput(raises=raises) for _ in range(count)]

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int) -> _FileInput:
        return self._items[index]


class _Response:
    def __init__(self, status: int = 200):
        self.status = status


# --- service_type (業務) fakes ------------------------------------------------------------------
#
# service_type is a *radio group*, not a `<select>` -- see storefront_offer._select_service_type,
# shared by both _apply() and _fill_create_form(). These fakes model exactly the subset of
# Playwright surface that helper actually touches: `page.locator(_SERVICE_TYPE_SELECTOR).all()`
# (a plain list, each item's grandparent innerText read via `.evaluate()`), `page.locator(f'label
# [for="{value}"]').click()`, and `page.expect_response(...)` around that click.


class _ServiceTypeRadio:
    """One radio in the group. `grandparent_text` models
    `e.parentElement.parentElement.innerText`; `value` models the radio's own `value` attribute,
    the id `label[for=<value>]` is keyed on in production. `checked` only ever flips true via a
    label click that `_FakeCreatePage` accepts (see `_click_service_type_label`) -- never merely
    because it was the one _select_service_type happened to match."""

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


class _ServiceTypeRadioGroup:
    """What `page.locator(_SERVICE_TYPE_SELECTOR)` resolves to -- `.all()` only, the one method
    _select_service_type actually calls on it."""

    def __init__(self, radios: list[_ServiceTypeRadio]):
        self._radios = radios

    def all(self) -> list[_ServiceTypeRadio]:
        return list(self._radios)


class _ServiceTypeLabel:
    """What `page.locator(f'label[for="{value}"]')` resolves to. `radio` is `None` when `value`
    matches no known radio -- clicking it is a no-op, mirroring a real click on an empty locator
    resolving to nothing rather than raising here (production's own strict-mode Locator would
    raise instead, but nothing in this file exercises that shape)."""

    def __init__(self, page: "_FakeCreatePage", radio: _ServiceTypeRadio | None):
        self._page = page
        self._radio = radio

    def click(self, **_kwargs) -> None:
        self._page._click_service_type_label(self._radio)


class _NetworkResponse:
    def __init__(self, url: str, status: int):
        self.url = url
        self.status = status


class _ExpectResponse:
    """Models `page.expect_response(predicate)`: `.value` becomes the response produced by
    whatever ran inside the `with` block (here, the label click via
    `_FakeCreatePage._click_service_type_label`) once the block exits -- mirroring real
    Playwright's own "resolved on __exit__" contract closely enough for this fake."""

    def __init__(self, page: "_FakeCreatePage"):
        self._page = page
        self.value: _NetworkResponse | None = None

    def __enter__(self) -> "_ExpectResponse":
        return self

    def __exit__(self, *_exc) -> bool:
        self.value = self._page._last_service_type_response
        return False


class _FakeCreatePage:
    """A minimal stand-in for the Playwright page create_package() drives.

    `fields` maps an exact selector to a `_Field` (each already carrying its wizard `step`).
    `buttons` (for `page.locator("button")`) and the manual-creation button (for
    `page.get_by_text("手動でパッケージを作成する", exact=True)`, which `_step()` uses) are not
    step-gated -- only content fields are, since the manual button lives on the chooser page and
    submit buttons are discovered after the wizard is already fully walked.

    `stall_at`: a set of step indices at which clicking 次へ does nothing (current_step does not
    advance) -- models a validation failure that leaves the wizard stuck, which is exactly what
    create_step_stalled exists to name. `validation_errors`: text exposed via
    `page.locator("[class*='error']")`, always "visible" -- the *lowest*-priority tier
    `_create_validation_messages` scrapes, exactly the net the shipped bug's fix narrows.

    `aria_invalid_nodes`: `_Field`s exposed via `page.locator('[aria-invalid="true"]')` -- the
    *first*-priority tier. `role_alert_texts`: strings exposed via `page.locator('[role="alert"]')`
    -- the second tier. `committed_tag_count`: how many `[aria-label="削除"]` nodes exist, modelling
    the tag widget's own committed-chip delete buttons (see _apply's tag-clearing loop in
    production). `described_nodes`: id -> text, resolved via `page.locator(f"#{id}")`, for an
    aria-invalid node's `aria-describedby` target.

    `service_type_radios`: explicit radios for the 業務 group (see the fakes immediately above
    this class). When omitted, `fields` may still carry a plain *string* (not a `_Field`) under
    `_SERVICE_TYPE_SELECTOR` -- `_fields_for()` does exactly this -- and this constructor turns
    that into a single default radio matching it, value `"1"`, so every test that does not care
    about service_type selection specifically still gets a working one without wiring it through
    by hand. `service_type_api_status`/`service_type_click_checks` model the category API the
    label click triggers (see `_click_service_type_label`).
    """

    def __init__(
        self,
        *,
        fields: dict[str, _Field] | None = None,
        buttons: list[_Field] | None = None,
        manual_button_lands_on: str | None,
        after_submit_url: str | None = None,
        stall_at: set[int] | None = None,
        next_button_visible: bool = True,
        unnamed_textarea_count: int = 1,
        file_input_count: int = 4,
        file_upload_raises: bool = False,
        validation_errors: list[str] | None = None,
        aria_invalid_nodes: list[_Field] | None = None,
        role_alert_texts: list[str] | None = None,
        committed_tag_count: int = 0,
        described_nodes: dict[str, str] | None = None,
        last_step: int = 5,
        service_type_radios: list[_ServiceTypeRadio] | None = None,
        service_type_api_status: int = 200,
        service_type_click_checks: bool = True,
    ):
        self.url = "https://www.lancers.jp/myplan"
        self.goto_log: list[str] = []
        self.event_log: list[tuple[str, str, int]] = []
        fields = dict(fields or {})
        default_service_type_label = fields.pop(_SERVICE_TYPE_SELECTOR, None)
        self._fields = fields
        for field in self._fields.values():
            field.page = self
        if service_type_radios is not None:
            self._service_type_radios = list(service_type_radios)
        elif isinstance(default_service_type_label, str):
            self._service_type_radios = [_ServiceTypeRadio(grandparent_text=default_service_type_label, value="1")]
        else:
            self._service_type_radios = []
        self._service_type_api_status = service_type_api_status
        self._service_type_click_checks = service_type_click_checks
        self._last_service_type_response: _NetworkResponse | None = None
        self._buttons = buttons if buttons is not None else []
        self._manual_button_lands_on = manual_button_lands_on
        self._after_submit_url = after_submit_url
        self.current_step = 0
        self.last_step = last_step
        self.stall_at = stall_at or set()
        self._validation_errors = validation_errors or []
        self._aria_invalid_nodes = aria_invalid_nodes or []
        self._role_alert_texts = role_alert_texts or []
        self._committed_tag_count = committed_tag_count
        self._described_nodes = described_nodes or {}
        self._unnamed_textarea = _UnnamedTextarea(self, count=unnamed_textarea_count)
        self._file_inputs = _FileInputs(file_input_count, raises=file_upload_raises)

        def _click_manual() -> None:
            if self._manual_button_lands_on is not None:
                self.url = self._manual_button_lands_on

        self._manual_button = _Field(text="手動でパッケージを作成する", name="手動でパッケージを作成する")
        self._manual_button.click = lambda **_kwargs: (_click_manual(), setattr(self._manual_button, "clicks", self._manual_button.clicks + 1))[-1]

        def _click_next() -> None:
            self.event_log.append(("advance", "次へ", self.current_step))
            if self.current_step in self.stall_at:
                return
            if self.current_step < self.last_step:
                self.current_step += 1

        self._next_button = _Field(visible=next_button_visible, text="次へ", name="次へ")
        self._next_button.click = lambda **_kwargs: (_click_next(), setattr(self._next_button, "clicks", self._next_button.clicks + 1))[-1]
        # What get_by_text("次へ") resolves to -- consumed only by _create_advance_control_state's
        # own, unrelated stall-evidence read (click-target resolution itself now goes through the
        # clickable-control census via `page.locator("button")` above, not get_by_text). Defaults
        # to the real button itself; a test may still swap this to model get_by_text disagreeing
        # with the census (see the stall-evidence tests further down this file).
        self._next_button_text_node = self._next_button

        self._image_marker = _Field(step=4, text=_IMAGE_STEP_MARKER_TEXT, name="画像ほかマーカー")
        self._image_marker.page = self

    def goto(self, url: str, **_kwargs) -> _Response:
        self.goto_log.append(url)
        self.url = url
        return _Response(200)

    def locator(self, selector: str):
        if selector == "button":
            # The census now reads 次へ straight off `page.locator("button")` (see
            # storefront_offer._create_click_census), so the real production shape -- 次へ is
            # itself a real <button> among every other button on the page -- must be modelled
            # here too, not only via the separate get_by_text("次へ") this fake also still serves
            # (used by _create_advance_control_state's own, unrelated stall-evidence read).
            return _LocatorList([self._next_button, *self._buttons])
        if selector in ("a", 'input[type="submit"]', 'input[type="button"]', '[role="button"]'):
            # No fixture in this file needs a non-<button> census control by default; a test that
            # does overrides `page.locator` itself (see e.g.
            # test_click_next_reaches_a_next_button_expressed_as_a_link below).
            return _LocatorList([])
        if selector == "textarea:not([name])":
            return self._unnamed_textarea
        if selector == "[class*='error']":
            return _LocatorList([_Field(text=t) for t in self._validation_errors])
        if selector == '[aria-invalid="true"]':
            return _LocatorList(self._aria_invalid_nodes)
        if selector == '[role="alert"]':
            return _LocatorList([_Field(text=t) for t in self._role_alert_texts])
        if selector == '[aria-label="削除"]':
            return _LocatorList([_Field(text="") for _ in range(self._committed_tag_count)])
        if selector == 'input[type="file"]':
            return self._file_inputs
        if selector.startswith("#") and selector[1:] in self._described_nodes:
            return _LocatorList([_Field(text=self._described_nodes[selector[1:]])])
        if selector == _SERVICE_TYPE_SELECTOR:
            return _ServiceTypeRadioGroup(self._service_type_radios)
        if selector.startswith('label[for="') and selector.endswith('"]'):
            value = selector[len('label[for="'):-2]
            radio = next((r for r in self._service_type_radios if r.value == value), None)
            return _ServiceTypeLabel(self, radio)
        return self._fields.get(selector, _EmptyField())

    def get_by_text(self, label: str, exact: bool = True):
        if label == "手動でパッケージを作成する":
            return _LocatorList([self._manual_button])
        if label == "次へ":
            return _LocatorList([self._next_button_text_node])
        if label == _IMAGE_STEP_MARKER_TEXT and not exact:
            return _LocatorList([self._image_marker])
        return _LocatorList([])

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        pass

    def expect_response(self, predicate, timeout=None) -> _ExpectResponse:
        """Models `page.expect_response(predicate)` -- see `_ExpectResponse`'s own docstring.
        `predicate` is accepted but not consulted: in this fake, whatever runs inside the `with`
        block (a service_type label click) already determines the one response that fires, so
        there is nothing else for the predicate to filter."""
        return _ExpectResponse(self)

    def _click_service_type_label(self, radio: _ServiceTypeRadio | None) -> None:
        """What a real `label[for=<value>]` click does: fire the live category lookup
        (`/v1/project_store_api/project_category/<value>`) storefront_offer._select_service_type
        awaits via `expect_response`, and -- only when that response is 200 and
        `service_type_click_checks` allows it -- actually mark the radio checked. Logged into
        `event_log` the same way every other field's fill/select_option is, so ordering
        assertions (service_type selected after subcategory) work the same way they do for every
        other field in this file.
        """
        self.event_log.append(("click", "service_type", self.current_step))
        if radio is None:
            self._last_service_type_response = None
            return
        self._last_service_type_response = _NetworkResponse(
            f"https://www.lancers.jp/v1/project_store_api/project_category/{radio.value}",
            self._service_type_api_status,
        )
        if self._service_type_api_status == 200 and self._service_type_click_checks:
            radio.checked = True

    def wait_for_function(self, script: str, *, arg: str | None = None, timeout=None) -> None:
        """Models Playwright's real wait_for_function: production only ever calls this to wait
        for subcategory's own option label to actually be among category's live options (see
        storefront_offer.py). The selector is read straight out of the script string (the
        production call site embeds it as `[name="..."] option`), so this stays a faithful
        re-check against whatever `_fields_for()` actually gave that field -- never a second,
        hardcoded notion of which fields are dependent selects. No match in the script, or a field
        this page was never told about, is a no-op (mirrors every other selector this fake does
        not model); a field that *is* known but whose options never carry `arg` raises
        TimeoutError, exactly as a real page would when the condition never becomes true.
        """
        match = re.search(r'name="([^"]+)"', script)
        if match is None:
            return
        field = self._fields.get(f'[name="{match.group(1)}"]')
        if field is None:
            return
        labels = [option.inner_text() for option in field.locator("option").all()]
        if arg is not None and arg not in labels:
            raise TimeoutError(f"condition never became true: {arg!r} not in {labels}")

    def wait_for_url(self, pattern, timeout=None) -> None:
        # Mirrors real Playwright: a URL that already matches resolves immediately, regardless of
        # `_after_submit_url` -- this is what lets a test model "the just-clicked control already
        # produced the listing URL directly" via a per-button `.click` override (the established
        # convention below) without also configuring `_after_submit_url`. When it does not yet
        # match, `_after_submit_url` (if set) models the in-flight navigation finally landing --
        # unchanged from every pre-existing test's expectation. Neither raises TimeoutError,
        # modelling a wait that genuinely never resolves -- the short "did it already land"
        # probe `_await_create_listing_id` uses relies on exactly this to mean "not yet", not
        # "failed".
        if pattern.match(self.url):
            return
        if self._after_submit_url is not None:
            self.url = self._after_submit_url
            return
        raise TimeoutError(f"url never matched pattern: {self.url}")


def _complete_product(**overrides) -> dict:
    product = {
        "title_stem": "業務システムを開発し",
        "subtitle": "小規模チーム向けの業務システムを開発します",
        "category": "IT・プログラミング・開発",
        "subcategory": "システム開発（オーダーメイド）",
        "service_type": "Webアプリケーション構築",
        "industry": "IT・通信・インターネット",
        "tags": ["業務システム"],
        "notice": "ご相談内容を確認してから進めます。",
        "description": "業務システムを要件定義から設計・実装・納品まで一気通貫で対応します。" * 3,
        "plans": [
            {"description": "ライトプラン", "price_jpy": 100000, "delivery_days": 14},
            {"description": "スタンダードプラン", "price_jpy": 200000, "delivery_days": 21},
            {"description": "プレミアムプラン", "price_jpy": 300000, "delivery_days": 30},
        ],
    }
    product.update(overrides)
    return product


def _select_options_for(*labels: str) -> list[_Option]:
    """A placeholder plus one option per label -- enough for a stall-evidence test to read a real
    `selected_label` back, mirroring a real `<select>`'s placeholder-first shape."""
    options = [_Option("選択してください", "")]
    options.extend(_Option(label, str(index + 1)) for index, label in enumerate(labels))
    return options


def _fields_for(product: dict) -> dict[str, _Field | str]:
    fields = {
        '[name="ProjectPlanForm.title"]': _Field(step=0, name="title"),
        '[name="ProjectPlanForm.subtitle"]': _Field(step=0, name="subtitle"),
        '[name="___main_category_id"]': _Field(options=_select_options_for(product["category"]), step=0, name="category"),
        '[name="ProjectPlanForm.project_category_id"]': _Field(options=_select_options_for(product["subcategory"]), step=0, name="subcategory"),
        # Not a _Field: service_type is a radio group, not a select (see _ServiceTypeRadio and
        # friends above). This plain string is consumed by _FakeCreatePage.__init__ to build one
        # default matching radio -- see that constructor's own docstring.
        _SERVICE_TYPE_SELECTOR: product["service_type"],
        '[name="ProjectPlanForm.industry_type_id"]': _Field(options=_select_options_for(product["industry"]), step=0, name="industry"),
        '[name="MultiSelectTagSearch_ProjectPlanTagForm"]': _Field(step=0, name="tags"),
        '[name="ProjectPlanForm.notice_for_sale"]': _Field(step=3, name="notice"),
    }
    for index, _plan in enumerate(product["plans"]):
        prefix = f"ProjectPlanMenuForm[{index}]"
        fields[f'[name="{prefix}.description"]'] = _Field(step=1, name=f"{prefix}.description")
        fields[f'[name="{prefix}.delivery_time"]'] = _Field(options=_delivery_options(), step=1, name=f"{prefix}.delivery_time")
        fields[f'[name="{prefix}.price"]'] = _Field(step=1, name=f"{prefix}.price")
    return fields


# 1. A complete product walks every step and fills every observed field exactly once -----------


def test_complete_product_fills_every_observed_field_exactly_once():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    result = module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert fields['[name="ProjectPlanForm.title"]'].fills == [product["title_stem"]]
    assert fields['[name="ProjectPlanForm.subtitle"]'].fills == [product["subtitle"]]
    assert fields['[name="___main_category_id"]'].selected == [{"label": product["category"]}]
    assert fields['[name="ProjectPlanForm.project_category_id"]'].selected == [{"label": product["subcategory"]}]
    # service_type is a radio group, not a select (see _ServiceTypeRadio) -- the one default
    # radio _FakeCreatePage built from _fields_for()'s label must have ended up checked.
    [service_type_radio] = page._service_type_radios
    assert service_type_radio.grandparent_text == product["service_type"]
    assert service_type_radio.checked is True
    assert fields['[name="ProjectPlanForm.industry_type_id"]'].selected == [{"label": product["industry"]}]
    assert fields['[name="MultiSelectTagSearch_ProjectPlanTagForm"]'].fills == product["tags"]
    assert fields['[name="ProjectPlanForm.notice_for_sale"]'].fills == [product["notice"]]
    assert page._unnamed_textarea.fills == [product["description"]]

    for index, plan in enumerate(product["plans"]):
        prefix = f"ProjectPlanMenuForm[{index}]"
        assert fields[f'[name="{prefix}.description"]'].fills == [plan["description"]]
        assert fields[f'[name="{prefix}.price"]'].fills == [str(plan["price_jpy"])]
        selected = fields[f'[name="{prefix}.delivery_time"]'].selected
        assert selected == [{"value": str(plan["delivery_days"])}]

    # The wizard walked all the way to 公開 (step 5) and attached the avatar on 画像ほか.
    assert page.current_step == 5
    # A 次へ is visible on every step in this default fixture, including 画像ほか -- so the final
    # content step advances via 次へ here, exactly like every earlier step, and says so.
    assert result == {"image_attached": True, "advanced_via": "next_button"}
    assert page._file_inputs.nth(0).set_files_calls == ["/tmp/irrelevant.png"]


# 1b. A field belonging to a later step is never filled while that step is hidden --------------
#
# The guarantee itself lives in _Field.fill()/.select_option() (they self-check is_visible() and
# raise AssertionError otherwise, see the class docstring) -- every test driving
# _fill_create_form is therefore already exercising it. This test names the guarantee explicitly.
# Confirmed manually while writing this: temporarily reverting _fill_create_form to fill every
# field before any 次へ click makes this test fail with an AssertionError raised from inside
# _Field.fill (a step-1 field filled while current_step was still 0); restoring the step-walk
# makes it pass again.


def test_fields_are_never_filled_before_their_step_is_current():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    result = module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert result["image_attached"] is True


# 2. Steps are advanced in order, and each step's fields are filled before its 次へ is clicked --


def test_steps_advance_in_order_and_each_steps_fields_precede_its_next_click():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    advances = [step for action, name, step in page.event_log if name == "次へ"]
    assert advances == [0, 1, 2, 3, 4]  # one advance per step, strictly in wizard order

    # Every fill/select_option is logged with the step that was current the instant it fired;
    # that sequence must never decrease (a later-step fill preceding an earlier-step one would
    # mean a field got touched out of order).
    all_steps = [step for _action, _name, step in page.event_log]
    assert all_steps == sorted(all_steps)


# 3. An advance that does not arrive raises create_step_stalled naming the step, carrying any
#    on-page validation text -----------------------------------------------------------------


def test_stalled_advance_raises_create_step_stalled_with_validation_text():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(
        fields=fields, manual_button_lands_on=None,
        stall_at={0},  # 次へ from 基本情報 never actually advances the wizard
        validation_errors=["タイトルを入力してください"],
    )

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    message = str(excinfo.value)
    assert "create_step_stalled: 基本情報" in message
    assert "タイトルを入力してください" in message
    # Nothing belonging to 料金表 (step 1) was ever reached.
    assert fields['[name="ProjectPlanMenuForm[0].description"]'].fills == []


def test_missing_next_button_raises_create_step_stalled_named_next_button_missing():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None, next_button_visible=False)

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert "create_step_stalled: 基本情報: next_button_missing" in str(excinfo.value)


# 3a. The click target -- 次へ is resolved off the shared clickable-control census, reaching past
#     <button> and past visible text alone ------------------------------------------------------
#
# get_by_text(label, exact=True) used to resolve 次へ to the element whose own text equalled the
# label -- on a real button that is commonly a <span> sitting inside the actual <button>, so
# clicking it did nothing (indistinguishable from a stalled step from the caller's side). The
# search is now a census match instead (see storefront_offer._create_click_census /
# _create_controls_named): it enumerates real <button>/<a>/input[submit|button]/[role="button"]
# elements directly and reads each one's own accessible name (text/aria-label/title/value), so
# there is no longer a bare text node to click by mistake -- a <button>'s own aggregate text
# already includes whatever a child <span> contributes. These tests exercise
# _click_create_next_button directly against small hand-built _FakeCreatePage graphs, independent
# of the larger wizard-walking fixtures used elsewhere in this file.


def test_click_next_still_resolves_when_its_text_is_produced_by_a_child_span():
    """A real <button> whose displayed text comes from a child <span> is still matched by its own
    aggregate inner_text() -- the census never needs to climb to an ancestor because it already
    queries the real interactive element, not an arbitrary text node."""
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)

    module._click_create_next_button(page, "基本情報")

    assert page._next_button.clicks == 1


def test_click_next_reaches_a_next_button_expressed_as_a_link():
    """7. The census's reach extends past <button>: an <a> naming itself 次へ is found and
    clicked -- the same reach _create_submit_control's own search now gets (see the task this
    shipped from: a live wake's advance control was neither a <button> nor named by its own
    text)."""
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None, next_button_visible=False)
    link = _Field(text="次へ", name="次へ-link")
    original_locator = page.locator
    page.locator = lambda selector: _LocatorList([link]) if selector == "a" else original_locator(selector)

    module._click_create_next_button(page, "基本情報")

    assert link.clicks == 1


def test_click_next_matches_a_control_named_only_by_aria_label():
    """The same accessible-name reach _create_submit_control's search gets: a control with no
    visible text is still found and clicked, via aria-label alone."""
    module = _module()
    control = _Field(text="", name="次へ-aria", attrs={"aria-label": "次へ"})
    page = _FakeCreatePage(fields={}, buttons=[control], manual_button_lands_on=None, next_button_visible=False)

    module._click_create_next_button(page, "基本情報")

    assert control.clicks == 1


def test_click_next_raises_a_named_failure_on_ambiguous_visible_matches():
    module = _module()
    first = _Field(text="次へ", name="次へ-1")
    second = _Field(text="次へ", name="次へ-2")
    page = _FakeCreatePage(fields={}, buttons=[first, second], manual_button_lands_on=None, next_button_visible=False)

    with pytest.raises(module.OfferError) as excinfo:
        module._click_create_next_button(page, "基本情報")

    assert "create_step_stalled: 基本情報: next_button_missing" in str(excinfo.value)
    assert first.clicks == 0
    assert second.clicks == 0  # unchanged discipline: ambiguity never picks a nearest guess


def test_click_next_still_clicks_a_real_button_directly_unchanged():
    """The default case -- the text sits directly on the real <button>, exactly like every other
    test in this file's `_next_button` fixture -- still resolves and clicks normally."""
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)

    module._click_create_next_button(page, "基本情報")

    assert page._next_button.clicks == 1
    assert page.current_step == 1


def test_next_button_missing_lists_the_visible_controls_it_saw():
    """next_button_missing must say what it saw, not just that 次へ was absent -- the same
    discarding-what-you-saw defect _step()/_field() were fixed for. A live wake that hit this
    path (create_step_stalled: 画像ほか: next_button_missing) had nothing to work from until a
    human manually dumped the page's visible buttons; this is that dump, built into the report
    itself."""
    module = _module()
    buttons = [_Field(text="戻る"), _Field(text="下書き保存"), _Field(text="キャンセル")]
    page = _FakeCreatePage(fields={}, buttons=buttons, manual_button_lands_on=None, next_button_visible=False)

    with pytest.raises(module.OfferError) as excinfo:
        module._click_create_next_button(page, "基本情報")

    message = str(excinfo.value)
    assert "create_step_stalled: 基本情報: next_button_missing" in message
    assert "戻る" in message
    assert "下書き保存" in message
    assert "キャンセル" in message


# 3b. Stall evidence -- what create_step_stalled now reports beyond the bare validation text ----
#
# The live incident this shipped from: `create_step_stalled: 基本情報: 基本情報`. The "validation
# text" was the step's own stepper heading, scraped by the old broad `[class*='error']` net --
# useless, because it tells you which step stalled (already known) and nothing about why. These
# tests exercise `_create_step_evidence` (called by `_advance_create_step` on a stall) directly
# where that is the clearest way to isolate one behaviour, and once end-to-end through
# `_fill_create_form` to prove the wiring actually reaches production callers.


def test_stall_evidence_reports_every_field_the_current_step_owns_with_filled_state():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)
    fields['[name="ProjectPlanForm.title"]'].fill(product["title_stem"])
    # subtitle deliberately left empty -- the single most likely stall cause, and fully
    # observable without inferring anything.

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["step"] == "基本情報"
    by_name = {item["field"]: item for item in payload["fields"]}
    assert set(by_name) == {"title", "subtitle", "category", "subcategory", "industry", "tags"}
    assert by_name["title"] == {"field": "title", "present": True, "type": "text", "filled": True, "visible": True}
    assert by_name["subtitle"] == {"field": "subtitle", "present": True, "type": "text", "filled": False, "visible": True}


def test_stall_evidence_reports_absent_field_when_the_step_no_longer_carries_it():
    module = _module()
    # A field the step is supposed to own is simply not in the DOM at all -- itself evidence
    # the markup changed, distinct from "present but empty".
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)

    payload = json.loads(module._create_step_evidence(page, "確認事項"))

    by_name = {item["field"]: item for item in payload["fields"]}
    assert by_name["notice"] == {"field": "notice", "present": False, "count": 0}


def test_stall_evidence_select_reports_selected_label_and_placeholder_reads_as_empty():
    module = _module()
    category_options = _select_options_for("AI・プログラミング・システム開発")
    category_field = _Field(options=category_options, step=0, name="category")
    page = _FakeCreatePage(fields={'[name="___main_category_id"]': category_field}, manual_button_lands_on=None)

    # Nothing was ever selected -- a real unset <select> still reports its first (placeholder)
    # option as checked, which must read as NOT filled, not as an unreadable field.
    payload = json.loads(module._create_step_evidence(page, "基本情報"))
    by_name = {item["field"]: item for item in payload["fields"]}
    assert by_name["category"]["type"] == "select"
    assert by_name["category"]["selected_label"] == "選択してください"
    assert by_name["category"]["filled"] is False

    category_field.select_option(label="AI・プログラミング・システム開発")

    payload = json.loads(module._create_step_evidence(page, "基本情報"))
    by_name = {item["field"]: item for item in payload["fields"]}
    assert by_name["category"]["selected_label"] == "AI・プログラミング・システム開発"
    assert by_name["category"]["filled"] is True


def test_stall_evidence_captures_aria_invalid_message_via_its_describedby_target():
    module = _module()
    title_field = _Field(attrs={"aria-invalid": "true", "aria-describedby": "title-error"}, step=0, name="title")
    page = _FakeCreatePage(
        fields={'[name="ProjectPlanForm.title"]': title_field},
        manual_button_lands_on=None,
        aria_invalid_nodes=[title_field],
        described_nodes={"title-error": "タイトルは50文字以内で入力してください"},
        # A stepper-chrome error is also present, but aria-invalid outranks it -- tier C is
        # never even consulted when tier A finds something.
        validation_errors=["基本情報"],
    )

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["validation_messages"] == ["タイトルは50文字以内で入力してください"]


def test_stall_evidence_captures_role_alert_message_when_no_aria_invalid_present():
    module = _module()
    page = _FakeCreatePage(
        fields={}, manual_button_lands_on=None,
        role_alert_texts=["料金は必ず3プラン必要です"],
        validation_errors=["料金表"],  # stepper chrome; must not win over role=alert
    )

    payload = json.loads(module._create_step_evidence(page, "料金表"))

    assert payload["validation_messages"] == ["料金は必ず3プラン必要です"]


def test_stall_evidence_excludes_the_step_heading_and_names_that_nothing_qualified():
    """The regression this task shipped from: the only `[class*='error']` match was the current
    step's own heading text, and the old scrape reported it verbatim as if it were a complaint.
    Asserted directly, per the task: this must yield no_validation_message_found, not "基本情報"."""
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None, validation_errors=["基本情報"])

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["validation_messages"] == ["no_validation_message_found"]


def test_stall_evidence_includes_the_page_url():
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)
    page.url = module.ORIGIN + "/myplan/add?type=manual"

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["url"] == module.ORIGIN + "/myplan/add?type=manual"


def test_stall_evidence_reports_the_advance_control_found_and_its_text():
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["advance_control"] == {"found": True, "text": "次へ", "disabled": False, "outer_html": "<button>次へ</button>"}


def test_stall_evidence_payload_is_bounded_and_says_when_truncated():
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None, validation_errors=["エラー" * 2000])

    text = module._create_step_evidence(page, "基本情報")

    assert len(text) <= module._CREATE_STALL_PAYLOAD_MAX_CHARS
    assert "truncated" in text


def test_stall_evidence_short_payload_is_not_marked_truncated():
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None, validation_errors=["短いエラー"])

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert "truncated" not in payload


def test_stall_is_fail_closed_with_exactly_one_advance_attempt_and_no_partial_progress():
    """The fence stays fail-closed: no retry (次へ is clicked exactly once), nothing from a later
    step is ever touched, and the raised error still names the stalled step."""
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(
        fields=fields, manual_button_lands_on=None,
        stall_at={0},
        validation_errors=["タイトルを入力してください"],
    )

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert str(excinfo.value).startswith("create_step_stalled: 基本情報: ")
    assert page._next_button.clicks == 1  # no retry
    assert page.current_step == 0  # never advanced
    for index in range(3):
        prefix = f"ProjectPlanMenuForm[{index}]"
        assert fields[f'[name="{prefix}.description"]'].fills == []  # 料金表 never reached


def test_stalled_advance_error_message_embeds_the_full_evidence_payload():
    """End-to-end: the JSON _create_step_evidence builds is exactly what lands in the OfferError
    a real create_package() caller sees and reports to the wake/Telegram line."""
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(
        fields=fields, manual_button_lands_on=None,
        stall_at={0},
        validation_errors=["タイトルを入力してください"],
    )

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    prefix = "create_step_stalled: 基本情報: "
    message = str(excinfo.value)
    assert message.startswith(prefix)
    payload = json.loads(message[len(prefix):])
    assert payload["step"] == "基本情報"
    assert payload["validation_messages"] == ["タイトルを入力してください"]
    assert payload["url"] == page.url
    assert payload["advance_control"] == {"found": True, "text": "次へ", "disabled": False, "outer_html": "<button>次へ</button>"}
    by_name = {item["field"]: item for item in payload["fields"]}
    assert set(by_name) == {"title", "subtitle", "category", "subcategory", "industry", "tags"}
    # Every 基本情報 field was already filled before the stalled advance was even attempted.
    assert by_name["title"]["filled"] is True
    assert by_name["category"]["selected_label"] == product["category"]
    assert "tag_widget" in payload


# 3c. The report hole this task closes -- which step is *actually showing* -----------------
#
# A live diagnostics pass reported every 基本情報 field present/filled, a real advance_control,
# and no validation message -- and still could not tell "the click did nothing" apart from "the
# wizard advanced and the arrival selector (ProjectPlanMenuForm[0].description) is wrong". Both
# leave every 基本情報 field present and holding its value, because the six-step wizard keeps
# every step's fields in the DOM at once. These tests drive _create_step_evidence against a page
# whose `.content()` returns real, structurally wizard-shaped HTML -- the same shape
# skills/_shared/marketplace-core/tests/fixtures/form_observer/wizard.html models: a class
# token present on exactly 5 of 6 sibling step-wrappers -- so form_observer.observe_page runs
# its real structural detection. Nothing about step detection is reimplemented here.

_HIDING_CLASS = "_hidden_faketest_42"
_WIZARD_STEP_NAMES = ("基本情報", "料金表", "業務内容", "確認事項", "画像ほか", "公開")


def _wizard_step_fields_html(step_name: str, hidden_names: frozenset) -> str:
    def _tag(kind: str, name: str) -> str:
        style = ' style="display:none"' if name in hidden_names else ""
        if kind == "select":
            return f'<select name="{name}"{style}><option value="">選択してください</option></select>'
        return f'<input type="text" name="{name}"{style}>'

    if step_name == "基本情報":
        return "".join([
            _tag("input", "ProjectPlanForm.title"),
            _tag("input", "ProjectPlanForm.subtitle"),
            _tag("select", "___main_category_id"),
            _tag("select", "ProjectPlanForm.project_category_id"),
            _tag("select", "ProjectPlanForm.industry_type_id"),
            _tag("input", "MultiSelectTagSearch_ProjectPlanTagForm"),
        ])
    if step_name == "料金表":
        return "".join(
            _tag("input", f"ProjectPlanMenuForm[{i}].description")
            + _tag("select", f"ProjectPlanMenuForm[{i}].delivery_time")
            + _tag("input", f"ProjectPlanMenuForm[{i}].price")
            for i in range(3)
        )
    if step_name == "業務内容":
        style = ' style="display:none"' if "業務内容 textarea (unnamed)" in hidden_names else ""
        return f"<textarea{style}></textarea>"
    if step_name == "確認事項":
        return _tag("input", "ProjectPlanForm.notice_for_sale")
    if step_name == "画像ほか":
        return '<input type="file">'
    return '<input type="hidden" name="__submit_marker" value="1">'  # 公開: still field-bearing


def _wizard_html(current_step_index: int, hidden_names: frozenset = frozenset()) -> str:
    """A six-step wizard page. Every step's fields sit in the DOM at once; every step but
    `current_step_index` is wrapped in a div carrying `_HIDING_CLASS` -- present on exactly 5 of
    the 6 siblings, the structural signature form_observer._hiding_class_signature looks for
    (never hardcoded in production; this literal is only this test's stand-in for whatever build
    hash a real page happens to carry). `hidden_names` additionally force-hides specific named
    fields via their own inline style regardless of which step they belong to -- how the "wizard
    advanced but one field lags" test below is built.
    """
    panels = []
    for index, name in enumerate(_WIZARD_STEP_NAMES):
        classes = "step-panel" if index == current_step_index else f"step-panel {_HIDING_CLASS}"
        panels.append(f'<div class="{classes}"><h2>{name}</h2>{_wizard_step_fields_html(name, hidden_names)}</div>')
    return f'<div class="wizard">{"".join(panels)}</div>'


class _PositionalLocator:
    """Resolves visibility/bounding-box for one node found via form_observer's own
    `_Node.css_path()` -- the exact mechanism observe_page uses to re-locate a field live, and
    also used here to resolve a plain `[name="..."]` selector against the same parsed content.
    A node is "hidden" if it or any ancestor carries `_HIDING_CLASS` or an inline
    `display:none`/`visibility:hidden` style.
    """

    def __init__(self, node):
        self._node = node

    def _hidden(self) -> bool:
        node = self._node
        while node is not None and node.tag != "#root":
            style = (node.attrs.get("style") or "").replace(" ", "")
            if "display:none" in style or "visibility:hidden" in style:
                return True
            if _HIDING_CLASS in node.classes:
                return True
            node = node.parent
        return False

    def count(self) -> int:
        return 1

    def is_visible(self) -> bool:
        return not self._hidden()

    def bounding_box(self):
        return {"width": 0, "height": 0} if self._hidden() else {"width": 120, "height": 24}

    def input_value(self) -> str:
        # form_observer._requirement_filled's text/textarea branch -- a <textarea>'s value is
        # its own text content; every other native control's is its `value` attribute.
        if self._node.tag == "textarea":
            return self._node.text
        return self._node.attrs.get("value") or ""

    def locator(self, selector: str):
        # form_observer._requirement_filled's <select> branch: option:checked always resolves to
        # exactly one option in a real browser -- the one carrying `selected`, or the first
        # (placeholder) option when nothing has ever been explicitly chosen.
        if selector != "option:checked":
            raise NotImplementedError(selector)
        options = [child for child in self._node.children if child.tag == "option"]
        selected = [option for option in options if "selected" in option.attrs]
        chosen = selected[0] if selected else (options[0] if options else None)
        if chosen is None:
            return _OptionList([])
        return _OptionList([_Option(chosen.text, chosen.attrs.get("value") or "")])


_NAME_SELECTOR = re.compile(r'^\[name="([^"]+)"\]$')


class _ObservablePage(_FakeCreatePage):
    """A `_FakeCreatePage` that additionally serves `.content()` and resolves both the
    positional (`tag:nth-of-type(n) > ...`) locators form_observer.observe_page issues per field
    and plain `[name="..."]` selectors, against the same parsed static HTML -- enough Playwright
    surface for the real observer to run its real detection against a hand-built page. Step
    detection itself is never reimplemented here; only locator resolution is.
    """

    def __init__(self, *, content_html: str, **kwargs):
        super().__init__(**kwargs)
        self._content_html = content_html

    def content(self) -> str:
        return self._content_html

    def _resolve_positional(self, selector: str):
        root = _OBSERVER._parse_tree(self.content())
        node = root
        for part in selector.split(" > "):
            tag, rest = part.split(":nth-of-type(")
            index = int(rest.rstrip(")")) - 1
            matches = [child for child in node.children if child.tag == tag]
            if index >= len(matches):
                return None
            node = matches[index]
        return node

    def _resolve_by_name(self, name_value: str):
        root = _OBSERVER._parse_tree(self.content())
        for node in [root, *root.iter_descendants()]:
            if node.attrs.get("name") == name_value:
                return node
        return None

    def locator(self, selector: str):
        if "nth-of-type(" in selector:
            node = self._resolve_positional(selector)
            return _PositionalLocator(node) if node is not None else _EmptyField()
        name_match = _NAME_SELECTOR.match(selector)
        if name_match:
            node = self._resolve_by_name(name_match.group(1))
            return _PositionalLocator(node) if node is not None else _EmptyField()
        return super().locator(selector)


def test_stall_evidence_names_the_current_step_when_the_wizard_never_advanced():
    """1. The wizard is still showing 基本情報 -- the payload names it as the current step."""
    module = _module()
    page = _ObservablePage(content_html=_wizard_html(0), fields={}, manual_button_lands_on=None)

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["current_step"] == "基本情報"


def test_stall_evidence_names_the_current_step_when_the_wizard_advanced_but_arrival_field_lags():
    """2. The wizard has structurally advanced to 料金表, but the one field the arrival check
    was waiting for (ProjectPlanMenuForm[0].description) has not finished mounting. The old
    report -- present/filled only -- cannot tell this apart from case 1: both leave every
    基本情報 field present and holding its value. current_step must name 料金表 here. This is the
    whole point of the task -- asserted directly, not inferred from anything else in the
    payload. See test_current_step_derivation_is_not_vacuous below for proof this isn't vacuous.
    """
    module = _module()
    hidden = frozenset({"ProjectPlanMenuForm[0].description"})
    page = _ObservablePage(content_html=_wizard_html(1, hidden), fields={}, manual_button_lands_on=None)

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["current_step"] == "料金表"


def test_current_step_derivation_is_not_vacuous():
    """Per the task: prove test 2 above is not vacuous. Force the current-step derivation to
    always answer 基本情報, confirm the advanced-wizard scenario then reads 基本情報 (a wrong
    answer, proving the assertion above is capable of failing), then revert and confirm it
    reads 料金表 again."""
    module = _module()
    hidden = frozenset({"ProjectPlanMenuForm[0].description"})
    page = _ObservablePage(content_html=_wizard_html(1, hidden), fields={}, manual_button_lands_on=None)

    original = module._create_current_step_name
    module._create_current_step_name = lambda steps, fields_report: "基本情報"
    try:
        broken_payload = json.loads(module._create_step_evidence(page, "基本情報"))
    finally:
        module._create_current_step_name = original

    assert broken_payload["current_step"] == "基本情報"  # the stand-in always answers this -- wrong here

    payload = json.loads(module._create_step_evidence(page, "基本情報"))
    assert payload["current_step"] == "料金表"  # restored: the real derivation answers correctly


def test_stall_evidence_reports_per_field_visibility_present_but_hidden_vs_shown():
    """3. A present-but-hidden field (基本情報's own fields, while the wizard actually shows
    料金表) reports visible false; a field on the step that is actually showing reports visible
    true."""
    module = _module()
    hidden_page = _ObservablePage(content_html=_wizard_html(1), fields={}, manual_button_lands_on=None)
    shown_page = _ObservablePage(content_html=_wizard_html(0), fields={}, manual_button_lands_on=None)

    hidden_payload = json.loads(module._create_step_evidence(hidden_page, "基本情報"))
    shown_payload = json.loads(module._create_step_evidence(shown_page, "基本情報"))

    hidden_by_name = {item["field"]: item for item in hidden_payload["fields"]}
    shown_by_name = {item["field"]: item for item in shown_payload["fields"]}
    assert hidden_by_name["title"]["present"] is True
    assert hidden_by_name["title"]["visible"] is False
    assert shown_by_name["title"]["present"] is True
    assert shown_by_name["title"]["visible"] is True


def test_stall_evidence_reports_the_arrival_fields_visibility_in_both_states():
    """4. The arrival field's visibility appears in the payload, both while it is still hidden
    (the wizard-advanced-but-lagging scenario) and once it has become visible -- the single fact
    the task says settles which of the two causes actually happened."""
    module = _module()
    hidden_page = _ObservablePage(
        content_html=_wizard_html(1, frozenset({"ProjectPlanMenuForm[0].description"})),
        fields={}, manual_button_lands_on=None,
    )
    visible_page = _ObservablePage(content_html=_wizard_html(1), fields={}, manual_button_lands_on=None)

    hidden_payload = json.loads(module._create_step_evidence(
        hidden_page, "基本情報",
        lambda: hidden_page.locator('[name="ProjectPlanMenuForm[0].description"]'),
        "ProjectPlanMenuForm[0].description",
    ))
    visible_payload = json.loads(module._create_step_evidence(
        visible_page, "基本情報",
        lambda: visible_page.locator('[name="ProjectPlanMenuForm[0].description"]'),
        "ProjectPlanMenuForm[0].description",
    ))

    assert hidden_payload["arrival_field"] == {"field": "ProjectPlanMenuForm[0].description", "visible": False}
    assert visible_payload["arrival_field"] == {"field": "ProjectPlanMenuForm[0].description", "visible": True}


def test_stall_evidence_reports_the_observers_inferred_wrapper_class():
    """5. The observer's inferred wrapper class appears in the payload, so a build that changes
    the CSS-module hash is still visible rather than silently breaking detection."""
    module = _module()
    page = _ObservablePage(content_html=_wizard_html(0), fields={}, manual_button_lands_on=None)

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["wrapper_class"] == _HIDING_CLASS
    assert payload["is_wizard"] is True


def test_stall_evidence_survives_an_observer_failure_and_names_it():
    """6. If the observer raises (here: the fake page carries no `.content()` at all, exactly
    like every other page fake in this file), the payload still carries every other key and
    names the observer failure -- a diagnostic must never become the thing that fails."""
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)  # no .content()
    fields['[name="ProjectPlanForm.title"]'].fill(product["title_stem"])

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["observer_error"] is not None
    assert payload["current_step"] is None
    assert payload["wrapper_class"] is None
    assert payload["is_wizard"] is None
    # every key the report already promised is still present
    assert payload["step"] == "基本情報"
    assert "fields" in payload
    assert "validation_messages" in payload
    assert "advance_control" in payload
    assert "tag_widget" in payload
    assert "arrival_field" in payload
    assert payload["step_requirements"] == []


# 3e. step_requirements -- every required/optional control the visible step carries, read by
# form_observer straight off the DOM, never from _CREATE_STEP_FIELDS' own six known names -----
#
# The task this shipped from: `fields` only ever enumerates the six controls this file already
# knows to fill, so a seventh required control on 基本情報 -- especially a custom widget that is
# not a native input/textarea/select -- would never show up in any report this file could build
# by itself. _requirements_wizard_html below models exactly that: 基本情報 carries three native
# controls (title/subtitle/category, mirroring three of the six known ones) plus one this file's
# own _CREATE_STEP_FIELDS never names at all (対応可能日, backed by nothing but a <div> widget);
# 料金表 (hidden, wrapped in the same structural signature form_observer._hiding_class_signature
# looks for) carries a required native control that must never surface, since its step is not the
# one showing. Step detection and requirement extraction are both the real form_observer code --
# nothing about either is reimplemented here.

_REQUIREMENTS_HIDING_CLASS = "_hidden_faketest_requirements_7"


def _requirements_wizard_html() -> str:
    basic_info = (
        '<div class="field"><label>タイトル<span>必須</span></label>'
        '<input type="text" name="ProjectPlanForm.title" value="サンプルタイトル"></div>'
        '<div class="field"><label>サブタイトル<span>任意</span></label>'
        '<input type="text" name="ProjectPlanForm.subtitle"></div>'
        '<div class="field"><label>カテゴリー<span>必須</span></label>'
        '<select name="___main_category_id"><option value="">選択してください</option>'
        '<option value="1">AI・プログラミング・システム開発</option></select></div>'
        '<div class="field"><label>対応可能日<span>必須</span></label>'
        '<div class="custom-widget" role="combobox"></div></div>'
    )
    pricing = (
        '<div class="field"><label>納期<span>必須</span></label>'
        '<input type="text" name="ProjectPlanMenuForm[0].delivery_time"></div>'
    )
    return (
        '<div class="wizard">'
        f'<div class="step-panel"><h2>基本情報</h2>{basic_info}</div>'
        f'<div class="step-panel {_REQUIREMENTS_HIDING_CLASS}"><h2>料金表</h2>{pricing}</div>'
        "</div>"
    )


def _requirements_payload(module) -> dict:
    page = _ObservablePage(content_html=_requirements_wizard_html(), fields={}, manual_button_lands_on=None)
    return json.loads(module._create_step_evidence(page, "基本情報"))


def test_step_requirements_reports_a_required_control_the_adapter_never_fills():
    """This is the case the whole task exists for: 対応可能日 is a required custom widget none of
    _CREATE_STEP_FIELDS' six known names cover, and it still shows up here with its visible
    label."""
    module = _module()

    labels = {entry["label"] for entry in _requirements_payload(module)["step_requirements"]}

    assert "対応可能日" in labels


def test_step_requirements_required_select_holding_only_its_placeholder_reports_empty():
    module = _module()

    by_label = {entry["label"]: entry for entry in _requirements_payload(module)["step_requirements"]}

    assert by_label["カテゴリー"]["tag"] == "select"
    assert by_label["カテゴリー"]["required"] is True
    assert by_label["カテゴリー"]["filled"] is False


def test_step_requirements_custom_widget_reports_present_but_unreadable_not_filled():
    module = _module()

    by_label = {entry["label"]: entry for entry in _requirements_payload(module)["step_requirements"]}

    assert by_label["対応可能日"]["readable"] is False
    assert by_label["対応可能日"]["filled"] is None


def test_step_requirements_only_reports_the_visible_steps_controls():
    module = _module()

    by_label = {entry["label"]: entry for entry in _requirements_payload(module)["step_requirements"]}

    assert "納期" not in by_label  # belongs to 料金表, hidden behind _REQUIREMENTS_HIDING_CLASS
    assert by_label["タイトル"]["required"] is True  # belongs to the visible 基本情報 step


def test_step_requirements_is_not_vacuous_when_restricted_to_the_adapters_known_names():
    """Per the task: prove the previous test is not vacuous. Restrict the observer's own
    candidate enumeration to labels the adapter's _CREATE_STEP_FIELDS already knows about
    (mirroring the six known field names at this fixture's smaller scale), confirm 対応可能日 then
    fails to appear, revert, confirm it is reported again."""
    module = _module()
    observer_module = module._reach_form_observer()
    known_labels = {"タイトル", "サブタイトル", "カテゴリー"}
    original = observer_module._step_requirement_candidates
    observer_module._step_requirement_candidates = lambda root: [
        candidate for candidate in original(root) if candidate["label"] in known_labels
    ]
    try:
        restricted_labels = {entry["label"] for entry in _requirements_payload(module)["step_requirements"]}
        assert "対応可能日" not in restricted_labels
    finally:
        observer_module._step_requirement_candidates = original

    restored_labels = {entry["label"] for entry in _requirements_payload(module)["step_requirements"]}
    assert "対応可能日" in restored_labels


# 3f. The advance control's outerHTML, hard-truncated -------------------------------------------


def test_stall_evidence_includes_the_advance_controls_truncated_outer_html():
    module = _module()
    long_html = '<button type="submit" class="c-btn c-btn--primary" data-testid="next">' + ("次へ" * 200) + "</button>"
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)
    page._next_button = _Field(visible=True, text="次へ", name="次へ", outer_html=long_html)
    page._next_button_text_node = page._next_button

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    outer_html = payload["advance_control"]["outer_html"]
    assert outer_html.startswith('<button type="submit"')
    assert outer_html.endswith(module._CREATE_STALL_TRUNCATION_MARKER)
    assert len(outer_html) == module._CREATE_OUTER_HTML_MAX_CHARS + len(module._CREATE_STALL_TRUNCATION_MARKER)


def test_stall_evidence_short_outer_html_is_not_truncated():
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)
    page._next_button = _Field(visible=True, text="次へ", name="次へ", outer_html='<button type="submit">次へ</button>')
    page._next_button_text_node = page._next_button

    payload = json.loads(module._create_step_evidence(page, "基本情報"))

    assert payload["advance_control"]["outer_html"] == '<button type="submit">次へ</button>'


# 3d. Fail-closed is unchanged by any of the above: exactly one advance click, no later-step
# field ever written, and the raised error still names the stalled step -- already proven by
# test_stall_is_fail_closed_with_exactly_one_advance_attempt_and_no_partial_progress above,
# which drives the real _FakeCreatePage (no `.content()`) through the real _fill_create_form and
# still passes unmodified; nothing about the observer wiring changes that fence.


# 4. The unnamed-textarea locator raises a named error for zero or more than one match ---------


def test_unnamed_textarea_locator_raises_when_absent():
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None, unnamed_textarea_count=0)

    with pytest.raises(module.OfferError) as excinfo:
        module._create_business_textarea(page)

    assert "create_business_textarea_invalid: count=0" in str(excinfo.value)


def test_unnamed_textarea_locator_raises_when_duplicated():
    module = _module()
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None, unnamed_textarea_count=2)

    with pytest.raises(module.OfferError) as excinfo:
        module._create_business_textarea(page)

    assert "create_business_textarea_invalid: count=2" in str(excinfo.value)


# 5. A description over the 2000-char cap is rejected before the browser is touched ------------


def test_description_over_cap_rejected_before_any_navigation():
    module = _module()
    product = _complete_product(description="あ" * 2001)
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    assert "create_field_invalid: description" in str(excinfo.value)
    assert "2001" in str(excinfo.value)
    assert page.goto_log == []


# 6. A missing description is rejected by _require_create_fields -------------------------------


def test_missing_description_rejected_by_require_create_fields():
    module = _module()
    product = _complete_product()
    del product["description"]

    with pytest.raises(module.OfferError) as excinfo:
        module._require_create_fields(product)

    assert "create_field_missing: description" in str(excinfo.value)


# 7. Missing/unusable file inputs yield image_attached: false, not a failure --------------------


def test_missing_file_inputs_yield_image_attached_false_without_raising():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None, file_input_count=0)

    result = module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert result == {"image_attached": False, "advanced_via": "next_button"}
    assert page.current_step == 5  # the wizard still reached 公開


def test_file_upload_failure_yields_image_attached_false_without_raising():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None, file_input_count=4, file_upload_raises=True)

    result = module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert result == {"image_attached": False, "advanced_via": "next_button"}


# 2 (legacy numbering). A delivery_days with no matching option raises, naming the value and
#    options; nothing selected -------------------------------------------------------------------


def test_unmatched_delivery_days_raises_and_selects_nothing():
    module = _module()
    product = _complete_product()
    product["plans"][1]["delivery_days"] = 18  # not in LANCERS_DELIVERY_DAYS / the options seen
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    message = str(excinfo.value)
    assert "create_delivery_time_unmatched" in message
    assert "18" in message
    assert "14日" in message  # one of the options actually seen is named in the error

    # The failing plan's own delivery select never got a selection, and the field after it
    # (price) was never reached either -- the fill stops the instant the mismatch is found.
    assert fields['[name="ProjectPlanMenuForm[1].delivery_time"]'].selected == []
    assert fields['[name="ProjectPlanMenuForm[1].price"]'].fills == []
    assert fields['[name="ProjectPlanMenuForm[2].description"]'].fills == []


# 2b. service_type (業務, the seventh required control) is a radio group selected right after
#     subcategory, by grandparent text (see storefront_offer._select_service_type, shared with
#     _apply()); a label matching no radio raises a named error listing every option seen --------

def test_service_type_is_selected_after_subcategory_by_label():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    subcategory_step = fields['[name="ProjectPlanForm.project_category_id"]'].selected
    assert subcategory_step == [{"label": product["subcategory"]}]
    [service_type_radio] = page._service_type_radios
    assert service_type_radio.grandparent_text == product["service_type"]
    assert service_type_radio.checked is True
    # service_type was chosen after subcategory in the event log (fill order matters: it is a
    # dependent of subcategory, exactly like subcategory is a dependent of category).
    subcategory_index = next(i for i, e in enumerate(page.event_log) if e[1] == "subcategory")
    service_type_index = next(i for i, e in enumerate(page.event_log) if e[1] == "service_type")
    assert subcategory_index < service_type_index


def test_unmatched_service_type_raises_and_lists_every_option_seen():
    module = _module()
    # The live radio group only ever offers the default product's own service_type label; asking
    # for a different one models a catalogue overlay whose service_type Lancers' subcategory does
    # not actually offer.
    fields = _fields_for(_complete_product())
    product = _complete_product(service_type="バグ修正")
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    message = str(excinfo.value)
    assert "form_changed: create:service_type" in message
    assert "バグ修正" in message
    assert "found=0" in message
    assert "Webアプリケーション構築" in message  # the one real option actually seen is named

    # industry (the field immediately after service_type) was never reached, and the one real
    # radio was never checked.
    assert fields['[name="ProjectPlanForm.industry_type_id"]'].selected == []
    [service_type_radio] = page._service_type_radios
    assert service_type_radio.checked is False


# 3. A missing required product field raises, naming the field, before any navigation ----------


def test_missing_required_field_raises_before_any_navigation():
    module = _module()
    product = _complete_product()
    del product["notice"]
    page = _FakeCreatePage(fields={}, manual_button_lands_on=module.ORIGIN + "/myplan/add?type=manual")

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    assert "create_field_missing: notice" in str(excinfo.value)
    assert page.goto_log == []


def test_missing_plan_slot_raises_before_any_navigation():
    module = _module()
    product = _complete_product()
    product["plans"] = product["plans"][:2]
    page = _FakeCreatePage(fields={}, manual_button_lands_on=None)

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    assert "create_field_missing: plans" in str(excinfo.value)
    assert page.goto_log == []


# 4. Not landing on ?type=manual raises rather than filling ------------------------------------


def test_not_landing_on_manual_type_raises_without_filling():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    # Clicking the manual button does nothing to page.url -- simulates the chooser not routing
    # as expected (a changed form, a blocked click, an intermediate interstitial).
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    assert "create_route_invalid" in str(excinfo.value)
    assert fields['[name="ProjectPlanForm.title"]'].fills == []
    assert page.goto_log == [module.ORIGIN + "/myplan/add"]


# 5. No submit-looking button raises, and the error lists the buttons that were present --------


def test_no_matching_submit_button_lists_the_buttons_present():
    module = _module()
    buttons = [_Field(text="プレビュー"), _Field(text="タイトルのコツ")]

    with pytest.raises(module.OfferError) as excinfo:
        module._create_submit_control(_FakeCreatePage(buttons=buttons, manual_button_lands_on=None))

    message = str(excinfo.value)
    assert "create_submit_control_missing" in message
    assert "プレビュー" in message
    assert "タイトルのコツ" in message


def test_more_than_one_matching_submit_button_also_raises():
    module = _module()
    buttons = [_Field(text="保存する"), _Field(text="公開する")]

    with pytest.raises(module.OfferError) as excinfo:
        module._create_submit_control(_FakeCreatePage(buttons=buttons, manual_button_lands_on=None))

    assert "create_submit_control_missing" in str(excinfo.value)


# 5a. The submit search matches an accessible name from any source, not only visible text --------
#
# The incident this task shipped from: create_submit_control_missing: buttons=[''] -- one visible
# <button>, empty text. _create_submit_control's search used to look only at <button>.inner_text();
# it now matches any census control's accessible name (see form_observer.clickable_accessible_names
# -- text, aria-label, title, or value) against _CREATE_SUBMIT_LABELS.


def test_submit_control_matches_a_control_named_only_by_aria_label():
    module = _module()
    control = _Field(text="", name="submit-aria", attrs={"aria-label": "公開する"})
    page = _FakeCreatePage(buttons=[control], manual_button_lands_on=None, next_button_visible=False)

    found = module._create_submit_control(page)

    assert found is control


def test_submit_control_matches_a_control_named_only_by_value():
    """An input[type=submit] commonly carries its label in `value`, never in inner_text() at
    all -- get_attribute("value") is exactly what form_observer.clickable_controls reads (see
    that module), and _create_submit_control matches against it the same way it matches text."""
    module = _module()
    control = _Field(text="", name="submit-value", attrs={"value": "送信"})
    page = _FakeCreatePage(buttons=[control], manual_button_lands_on=None, next_button_visible=False)

    found = module._create_submit_control(page)

    assert found is control


# 8. A "no control matched" failure carries the current step and the URL, not only the buttons --


def test_submit_control_missing_failure_carries_the_url():
    """create_submit_control_missing must say where the page was, not only what it saw -- after
    an image upload the wizard may not be where the walk thinks it is."""
    module = _module()
    page = _FakeCreatePage(buttons=[_Field(text="プレビュー")], manual_button_lands_on=None, next_button_visible=False)
    page.url = module.ORIGIN + "/myplan/add?type=manual"

    with pytest.raises(module.OfferError) as excinfo:
        module._create_submit_control(page)

    message = str(excinfo.value)
    payload = json.loads(message[message.index("{"):])
    assert payload["url"] == module.ORIGIN + "/myplan/add?type=manual"


def test_next_button_missing_failure_carries_the_current_step():
    """next_button_missing's own failure carries the observer's own read of which step is
    actually showing (per _create_observer_step_state -- the same fact _create_step_evidence
    already reports for a stalled arrival), not only the controls it saw."""
    module = _module()
    page = _ObservablePage(
        content_html=_wizard_html(0), fields={}, buttons=[], manual_button_lands_on=None,
        next_button_visible=False,
    )

    with pytest.raises(module.OfferError) as excinfo:
        module._click_create_next_button(page, "基本情報")

    message = str(excinfo.value)
    payload = json.loads(message[message.index("{"):])
    assert payload["step"] == "基本情報"


# 6c. A created listing whose public page actually matches yields action: created --------------
#
# Every other successful-submit test in this file deliberately stops at publication_uncertain,
# per their own comments, because _FakeCreatePage models the wizard, not a rendered
# /menu/detail/<id> page (canonical/og/plan-sidebar markup -- see
# test_public_readback_gates.py's _PublicPage for that side, and its own test coverage of
# _public()'s comparisons in isolation). This test drives the exact same wizard walk to a real
# submit, then monkeypatches only _public() itself to report "the public page matches" -- proving
# create_package()'s own merge (`_public(...) | {"action": "created", ...} | fill_result`) is
# unaffected by this task's fix and still reports success with the new listing's id.


def test_create_package_reports_created_with_listing_id_when_public_readback_matches(monkeypatch):
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    submit_button = _Field(text="保存する")
    manual_url = module.ORIGIN + "/myplan/add?type=manual"
    created_url = module.ORIGIN + "/myplan/999999/edit"
    page = _FakeCreatePage(
        fields=fields, buttons=[submit_button], manual_button_lands_on=manual_url,
        after_submit_url=created_url,
    )
    submit_button.click = lambda **_kwargs: (setattr(page, "url", created_url), setattr(submit_button, "clicks", submit_button.clicks + 1))[-1]
    monkeypatch.setattr(
        module, "_public",
        lambda _page, _product, **_kwargs: {"ok": True, "aligned": True, "mismatched_fields": [], "canonical_url": module.ORIGIN + "/menu/detail/999999"},
    )

    result = module.create_package(page, product, Path("/tmp/irrelevant.png"))

    assert result["action"] == "created"
    assert result["listing_external_id"] == "999999"
    assert result["aligned"] is True
    assert result["mismatched_fields"] == []


# 10. A census large enough to need it says so, rather than silently dropping controls ----------


def test_submit_control_missing_reports_truncation_for_a_large_census():
    module = _module()
    buttons = [_Field(text=f"プレビュー{index}" * 20) for index in range(80)]
    page = _FakeCreatePage(buttons=buttons, manual_button_lands_on=None, next_button_visible=False)

    with pytest.raises(module.OfferError) as excinfo:
        module._create_submit_control(page)

    message = str(excinfo.value)
    assert message.endswith(module._CREATE_STALL_TRUNCATION_MARKER)
    assert len(message) < sum(len(b._text) for b in buttons)


# 6. A submit that succeeds but whose public readback fails yields publication_uncertain -------


def test_successful_submit_with_failed_readback_is_publication_uncertain():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    submit_button = _Field(text="保存する")
    manual_url = module.ORIGIN + "/myplan/add?type=manual"
    created_url = module.ORIGIN + "/myplan/999999/edit"
    page = _FakeCreatePage(
        fields=fields,
        buttons=[submit_button],
        manual_button_lands_on=manual_url,
        after_submit_url=created_url,
    )
    # The submit click itself lands the new listing's id in the URL, exactly as the discovered
    # button doing its real job would.
    submit_button.click = lambda **_kwargs: (setattr(page, "url", created_url), setattr(submit_button, "clicks", submit_button.clicks + 1))[-1]

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    # Evidence-bearing now: the underlying canonical_mismatch names what it saw (counts and
    # both observed values), not just its own bare name -- see storefront_offer._public.
    assert str(excinfo.value).startswith("publication_uncertain: canonical_mismatch: ")
    assert "canonical_count=0" in str(excinfo.value)
    # The specific readback failure is chained, not discarded -- a wake hitting this path can
    # act on "canonical_mismatch" instead of a bare, anonymous "publication_uncertain".
    assert isinstance(excinfo.value.__cause__, module.OfferError)
    assert str(excinfo.value.__cause__).startswith("canonical_mismatch: ")
    # The public page was actually visited (as _public() always does) before giving up, and the
    # wizard walked all the way through before the submit control was even looked for.
    assert page.goto_log[-1] == module.ORIGIN + "/menu/detail/999999"
    assert page.current_step == 5


def test_create_listing_id_unresolved_when_the_post_submit_url_carries_no_id():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    submit_button = _Field(text="保存する")
    manual_url = module.ORIGIN + "/myplan/add?type=manual"
    unresolvable_url = module.ORIGIN + "/myplan/add/complete"
    page = _FakeCreatePage(
        fields=fields,
        buttons=[submit_button],
        manual_button_lands_on=manual_url,
        after_submit_url=unresolvable_url,
    )
    submit_button.click = lambda **_kwargs: setattr(page, "url", unresolvable_url)

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    assert "create_listing_id_unresolved" in str(excinfo.value)


# 6b. The final content step (画像ほか) accepts either a 次へ or the discovered submit control ---
#
# A live wake stalled here with create_step_stalled: 画像ほか: next_button_missing -- the live DOM
# read that shipped from this task's incident found the wizard is 基本情報 -> 料金表 -> 業務内容 ->
# 確認事項 -> 画像ほか -> 公開, and 画像ほか has no 次へ. _advance_from_final_content_step (see its
# own docstring) tries 次へ first and falls back to the submit control _create_submit_control
# discovers only when no unambiguous 次へ is present, recording which one fired as
# `advanced_via`. Every earlier step is untouched -- still driven only by
# _click_create_next_button, which never falls back to a submit control (the safety property
# below).
#
# _hide_next_button_only_at models "every step but this one still carries a 次へ": the fake's
# single shared 次へ field otherwise has one page-wide visible/hidden flag
# (`next_button_visible`), which cannot express "hidden at 画像ほか but present everywhere else"
# on its own.


def _hide_next_button_only_at(page: "_FakeCreatePage", step: int) -> None:
    page._next_button.is_visible = lambda: page.current_step != step


def test_earlier_step_never_falls_back_to_a_submit_control_when_its_next_button_is_missing():
    """The safety property: an earlier step (基本情報) with no 次へ but a visible submit-labeled
    control must still fail, never click that control -- clicking a submit control on an earlier
    step would publish a half-filled listing. Proven not vacuous manually, per the task:
    temporarily letting _click_create_next_button fall back to _create_submit_control on any step
    makes this test fail (the submit button gets clicked instead of the expected error being
    raised); reverting that change makes it pass again."""
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    submit_button = _Field(text="公開する")
    page = _FakeCreatePage(fields=fields, buttons=[submit_button], manual_button_lands_on=None)
    _hide_next_button_only_at(page, 0)  # 基本情報 itself has no 次へ

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert "create_step_stalled: 基本情報: next_button_missing" in str(excinfo.value)
    assert submit_button.clicks == 0  # never clicked as a fallback
    assert page.current_step == 0  # never advanced


def test_final_content_step_advances_via_next_button_when_present():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)  # 次へ visible everywhere

    result = module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert result["advanced_via"] == "next_button"
    assert page.current_step == 5  # actually advanced to 公開


def test_final_content_step_advances_via_submit_control_when_next_button_absent():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    submit_button = _Field(text="公開する")
    page = _FakeCreatePage(fields=fields, buttons=[submit_button], manual_button_lands_on=None)
    _hide_next_button_only_at(page, 4)  # 画像ほか itself has no 次へ -- the live incident's shape

    result = module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert result["advanced_via"] == "submit_control"
    assert submit_button.clicks == 1
    # Whether this control leads to 公開 or straight to a created listing is exactly what
    # create_package() itself must determine (see its own docstring) -- _fill_create_form makes
    # no assumption and therefore checks no structural arrival here.
    assert page.current_step == 4


def test_create_submit_labels_accepts_送信_observed_on_the_live_form():
    """送信 was observed live in a button dump of this exact form while diagnosing the
    next_button_missing stall (戻る/下書き保存/次へ/閉じる/キャンセル/送信 were all visible) --
    not a guess."""
    module = _module()
    button = _Field(text="送信")
    page = _FakeCreatePage(buttons=[button], manual_button_lands_on=None)

    control = module._create_submit_control(page)

    assert control is button


def test_final_content_step_neither_next_button_nor_submit_control_fails_closed_listing_controls():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    buttons = [_Field(text="プレビュー"), _Field(text="タイトルのコツ")]
    page = _FakeCreatePage(fields=fields, buttons=buttons, manual_button_lands_on=None)
    _hide_next_button_only_at(page, 4)

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    message = str(excinfo.value)
    assert "create_step_stalled: 画像ほか" in message
    assert "プレビュー" in message
    assert "タイトルのコツ" in message


def test_create_package_final_submit_that_lands_on_listing_url_needs_no_second_submit():
    """The submit control discovered on 画像ほか may create the listing directly -- when it does,
    create_package() must not go looking for (or clicking) a second submit control."""
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    manual_url = module.ORIGIN + "/myplan/add?type=manual"
    created_url = module.ORIGIN + "/myplan/999999/edit"
    submit_button = _Field(text="公開する")
    page = _FakeCreatePage(fields=fields, buttons=[submit_button], manual_button_lands_on=manual_url)
    _hide_next_button_only_at(page, 4)
    submit_button.click = lambda **_kwargs: (setattr(page, "url", created_url), setattr(submit_button, "clicks", submit_button.clicks + 1))[-1]

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    # _public() cannot succeed against this minimal fake (no canonical/og markup modelled) --
    # exactly the pre-existing publication_uncertain shape every other successful-submit test in
    # this file already exercises. What this test proves is which path got there.
    # Evidence-bearing now: the underlying canonical_mismatch names what it saw (counts and
    # both observed values), not just its own bare name -- see storefront_offer._public.
    assert str(excinfo.value).startswith("publication_uncertain: canonical_mismatch: ")
    assert "canonical_count=0" in str(excinfo.value)
    assert submit_button.clicks == 1  # the 画像ほか submit alone created the listing
    assert page.goto_log[-1] == module.ORIGIN + "/menu/detail/999999"


def test_create_package_final_submit_that_lands_on_another_step_continues_and_submits_there():
    """The submit control discovered on 画像ほか may instead only advance to a further 公開 step
    (the URL does not become a listing URL) -- create_package() must treat that as a step
    transition, not a failed creation, and submit again on whatever step is now showing."""
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    manual_url = module.ORIGIN + "/myplan/add?type=manual"
    created_url = module.ORIGIN + "/myplan/999999/edit"
    submit_button = _Field(text="公開する")
    page = _FakeCreatePage(fields=fields, buttons=[submit_button], manual_button_lands_on=manual_url)
    _hide_next_button_only_at(page, 4)

    # The first click (画像ほか's own discovered control) only advances to 公開 -- the URL does
    # not change. The same physical control is what 公開 itself then offers; only its second
    # click actually creates the listing.
    def _click(**_kwargs) -> None:
        submit_button.clicks += 1
        if submit_button.clicks >= 2:
            page.url = created_url
    submit_button.click = _click

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    # Evidence-bearing now: the underlying canonical_mismatch names what it saw (counts and
    # both observed values), not just its own bare name -- see storefront_offer._public.
    assert str(excinfo.value).startswith("publication_uncertain: canonical_mismatch: ")
    assert "canonical_count=0" in str(excinfo.value)
    assert submit_button.clicks == 2  # 画像ほか's own submit did not create it; 公開's did
    assert page.goto_log[-1] == module.ORIGIN + "/menu/detail/999999"


def test_create_listing_id_unresolved_when_final_submit_never_resolves_via_submit_control_path():
    """create_listing_id_unresolved still fires when the URL never becomes a listing URL, even
    when 画像ほか itself advanced via the submit-control fallback rather than 次へ (item 8's
    submit_control-path counterpart to the pre-existing next_button-path test above)."""
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    manual_url = module.ORIGIN + "/myplan/add?type=manual"
    unresolvable_url = module.ORIGIN + "/myplan/add/complete"
    submit_button = _Field(text="公開する")
    page = _FakeCreatePage(
        fields=fields, buttons=[submit_button], manual_button_lands_on=manual_url,
        after_submit_url=unresolvable_url,
    )
    _hide_next_button_only_at(page, 4)
    submit_button.click = lambda **_kwargs: setattr(submit_button, "clicks", submit_button.clicks + 1)

    with pytest.raises(module.OfferError) as excinfo:
        module.create_package(page, product, Path("/tmp/irrelevant.png"))

    assert "create_listing_id_unresolved" in str(excinfo.value)


# 7. _apply() is unchanged -----------------------------------------------------------------


def test_apply_edit_url_construction_is_unchanged():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'edit_url = f"{ORIGIN}/myplan/{listing_id}/edit"' in source


def test_apply_still_short_circuits_to_unchanged_when_aligned(monkeypatch):
    module = _module()
    product = {"listing_external_id": "555555", "superseded_listing_ids": []}
    public_calls: list[dict] = []

    def fake_public(_page, prod):
        public_calls.append(dict(prod))
        return {"ok": True, "aligned": True, "mismatched_fields": []}

    monkeypatch.setattr(module, "_public", fake_public)
    monkeypatch.setattr(
        module, "_reconcile_superseded",
        lambda _page, _ids: {"superseded_visible_count": 0, "status_effect_count": 0},
    )

    result = module._apply(object(), product, Path("/tmp/irrelevant.png"))

    assert result["action"] == "unchanged"
    assert len(public_calls) == 1  # _apply() still reads the public page before deciding anything


# 8. The creation path is not reachable from the existing apply flow without the explicit flag -


def test_run_never_calls_create_package():
    module = _module()
    run_source = inspect.getsource(module.run)
    assert "create_package(" not in run_source


def test_apply_cli_flag_never_invokes_create_package(monkeypatch):
    module = _module()
    called: list[object] = []
    monkeypatch.setattr(module, "create_package", lambda *a, **k: called.append((a, k)))
    monkeypatch.setattr(module, "run", lambda apply, product_path, state_path: {"ok": True})

    class _Delivery:
        delivery_uncertain = False
        pre_send_failed = False

    class _Reporter:
        @staticmethod
        def notify_storefront_wake(_result):
            return _Delivery()

    monkeypatch.setattr(module, "_load", lambda *_a, **_k: _Reporter())

    exit_code = module.main(["--apply"])

    assert exit_code == 0
    assert called == []


def test_create_package_flag_invokes_run_create_only(monkeypatch):
    module = _module()
    calls: list[tuple] = []
    monkeypatch.setattr(module, "run_create", lambda product_path, state_path: calls.append((product_path, state_path)) or {"ok": True})
    monkeypatch.setattr(module, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("run() must not be called for --create-package")))

    exit_code = module.main(["--create-package"])

    assert exit_code == 0
    assert len(calls) == 1


def test_create_package_and_apply_are_mutually_exclusive():
    module = _module()
    with pytest.raises(SystemExit):
        module.main(["--apply", "--create-package"])


# 9. The real catalogue's mvp_web_app_build projection passes _require_create_fields, including
#    the new description requirement -------------------------------------------------------------


def test_real_catalog_mvp_web_app_build_passes_require_create_fields_with_description():
    module = _module()
    listing_catalog = module._reach_marketplace_core()
    catalog = listing_catalog.load(module.DEFAULT_CATALOG)

    product = module._family_create_product(listing_catalog, catalog, "mvp_web_app_build")

    assert isinstance(product.get("description"), str) and product["description"].strip()
    assert len(product["description"]) <= module._CREATE_DESCRIPTION_MAX_LENGTH
    module._require_create_fields(product)  # must not raise


# --- Catalogue-driven creation: select_catalog_family_to_create / run_catalog_create ----------
#
# The wake now has a decision, not just a capability: after the existing align/inspect chain in
# run() leaves nothing to do, pick one catalogue family with no live Lancers listing and create
# it. select_catalog_family_to_create() is pure (no browser); run_catalog_create() is the one
# browser-touching wrapper main() reaches for. These fixtures build a small three-family
# catalogue rather than depending on the real twenty-family one, so "a family is creatable" and
# "a family is not" can both be exercised directly -- the real catalogue's own grounding (every
# family's category/subcategory/industry/tags/notice) is covered separately in
# skills/_shared/marketplace-core/tests/test_listing_catalog.py.


def _fixture_tier(name: str, price_jpy: int, delivery_days: int) -> dict:
    return {"name": name, "price_jpy": price_jpy, "scope": f"{name}スコープ", "delivery_days": delivery_days}


def _fixture_family(family: str, *, category: str = "AI・プログラミング・システム開発",
                     extra_override: dict | None = None, drop_override_fields: tuple[str, ...] = ()) -> dict:
    override = {
        "category": category,
        "subcategory": "システム開発（オーダーメイド）",  # a future, filled-in overlay -- not the real catalog's shape
        "service_type": "Webアプリケーション構築",
        "industry": "IT・通信・インターネット",
        "tags": [family],
        "notice": f"{family}のご相談内容を確認してから進めます。",
    }
    if extra_override: override.update(extra_override)
    for field in drop_override_fields: override.pop(field, None)
    return {
        "id": family.replace("_", "-"),
        "family": family,
        # Long enough that title_ja minus "ます" clears Lancers' 25-character stem minimum
        # (listing_catalog.LANCERS_TITLE_STEM_MIN_LENGTH) for every family name this file uses.
        "title_ja": f"{family}という新しい業務システムの開発を一気通貫で対応します",
        "value_prop": f"{family}の価値提案。",
        "tiers": [
            _fixture_tier("ベーシック", 50000, 14),
            _fixture_tier("スタンダード", 100000, 21),
            _fixture_tier("プレミアム", 200000, 30),
        ],
        "deliverables": ["納品物"],
        "required_inputs": ["入力"],
        "faq": [],
        "platform_overrides": {
            "coconala": {"category": "IT相談・システム開発"},
            "lancers": override,
            "crowdworks": {"category": "システム開発・運用"},
        },
    }


def _write_fixture_catalog(tmp_path: Path, families: list[dict]) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"version": 1, "listings": families}, ensure_ascii=False), encoding="utf-8")
    return path


class _FakeTick:
    """Stands in for application_tick.py's browser-boundary surface. account_lock is a real
    contextmanager (not a mock) so nesting/deadlock bugs in the caller would show up as a hang,
    the same as the real fcntl.flock-backed one would -- it just never contends in tests.
    """

    CDP_URL = "http://127.0.0.1:0/fake-cdp"

    def __init__(self, *, account_ready: bool = True):
        self.account_ready = account_ready
        self.opened_pages = 0
        self.closed_pages = 0

    def account_lock(self, _path):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield

        return _cm()

    def _default_browser_factory(self, _cdp_url):
        return object()

    def _new_owned_page(self, _browser):
        self.opened_pages += 1
        return object()

    def _production_account_ready(self, _page):
        return self.account_ready

    def _close_owned_page(self, _page):
        self.closed_pages += 1
        return True

    def _stop_playwright_runtime(self, _runtime):
        return None


def _patch_browser_layer(module, monkeypatch, *, tick: "_FakeTick | None" = None, create_results=None):
    """create_results: either a single dict (every create_package() call returns it) or a list
    consumed in call order (one entry per family, in the order create_package() is invoked)."""
    tick = tick or _FakeTick()
    monkeypatch.setattr(module, "_load", lambda _name, _path: tick)
    calls: list[dict] = []

    def _fake_create_package(_page, product, _image):
        calls.append(dict(product))
        if isinstance(create_results, list):
            result = create_results[len(calls) - 1]
        else:
            result = create_results
        if isinstance(result, Exception):
            raise result
        return dict(result)

    monkeypatch.setattr(module, "create_package", _fake_create_package)
    return tick, calls


# 6. Selection picks a family with no live listing, and never one already recorded published ---


def test_select_picks_the_first_pending_family_in_catalogue_order(tmp_path):
    module = _module()
    catalog_path = _write_fixture_catalog(tmp_path, [_fixture_family("alpha"), _fixture_family("beta")])
    state_path = tmp_path / "application.json"

    selection = module.select_catalog_family_to_create(catalog_path, state_path)

    assert selection["action"] == "candidate_selected"
    assert selection["family"] == "alpha"
    assert selection["skipped"] == []


def test_select_never_picks_a_family_already_recorded_as_published(tmp_path):
    module = _module()
    catalog_path = _write_fixture_catalog(tmp_path, [_fixture_family("alpha"), _fixture_family("beta")])
    state_path = tmp_path / "application.json"
    module._write_catalog_listing(state_path, "alpha", {"listing_external_id": "111111"})

    selection = module.select_catalog_family_to_create(catalog_path, state_path)

    assert selection["action"] == "candidate_selected"
    assert selection["family"] == "beta"


# 7. Two wakes in a row do not create the same family twice ------------------------------------


def test_two_consecutive_wakes_create_two_different_families(tmp_path, monkeypatch):
    module = _module()
    catalog_path = _write_fixture_catalog(tmp_path, [_fixture_family("alpha"), _fixture_family("beta")])
    state_path = tmp_path / "application.json"
    tick, calls = _patch_browser_layer(
        module, monkeypatch,
        create_results=[
            {"ok": True, "listing_external_id": "100001", "canonical_url": "https://www.lancers.jp/menu/detail/100001"},
            {"ok": True, "listing_external_id": "100002", "canonical_url": "https://www.lancers.jp/menu/detail/100002"},
        ],
    )

    first = module.run_catalog_create(state_path, catalog_path)
    second = module.run_catalog_create(state_path, catalog_path)

    assert first["ok"] is True and first["family"] == "alpha" and first["listing_external_id"] == "100001"
    assert second["ok"] is True and second["family"] == "beta" and second["listing_external_id"] == "100002"
    assert len(calls) == 2  # create_package() was reached exactly once per wake, for a different family each time

    listings = module._read_catalog_listings(state_path)
    assert listings["alpha"]["listing_external_id"] == "100001"
    assert listings["beta"]["listing_external_id"] == "100002"

    # A third wake, with nothing left pending, must not touch the browser layer at all.
    third = module.run_catalog_create(state_path, catalog_path)
    assert third == {"action": "all_published", "skipped": []}
    assert len(calls) == 2


# 8. A family with an incomplete overlay is skipped, named, and does not block the others -------


def test_incomplete_overlay_is_skipped_and_named_without_blocking_a_later_family(tmp_path):
    module = _module()
    catalog_path = _write_fixture_catalog(
        tmp_path,
        [
            _fixture_family("broken", drop_override_fields=("notice",)),
            _fixture_family("fine"),
        ],
    )
    state_path = tmp_path / "application.json"

    selection = module.select_catalog_family_to_create(catalog_path, state_path)

    assert selection["action"] == "candidate_selected"
    assert selection["family"] == "fine"
    assert selection["skipped"] == [{"family": "broken", "reason": "create_field_missing: notice"}]


# 8b. A family whose overlay lacks service_type specifically -- the seventh required control this
#     lane only just learned about -- is skipped by name, and selection still advances to the
#     next complete family. This is the whole point of the module's overlay-skip design: an
#     unobserved-vocabulary family (most subcategories' service_type option lists have never been
#     read live) must never stall every family behind it.


def test_family_missing_service_type_is_skipped_and_queue_advances_to_the_next_complete_family(tmp_path):
    module = _module()
    catalog_path = _write_fixture_catalog(
        tmp_path,
        [
            _fixture_family("no_svc_type", drop_override_fields=("service_type",)),
            _fixture_family("grounded"),
        ],
    )
    state_path = tmp_path / "application.json"

    selection = module.select_catalog_family_to_create(catalog_path, state_path)

    assert selection["action"] == "candidate_selected"
    assert selection["family"] == "grounded"
    assert selection["skipped"] == [
        {"family": "no_svc_type", "reason": "create_field_missing: service_type"},
    ]
    # The selected candidate's own product actually carries service_type -- the queue did not
    # just skip past the incomplete family, it produced a fillable product for the next one.
    assert selection["product"]["service_type"] == "Webアプリケーション構築"


def test_run_catalog_create_reports_all_pending_incomplete_and_creates_nothing(tmp_path, monkeypatch):
    module = _module()
    catalog_path = _write_fixture_catalog(
        tmp_path,
        [_fixture_family("broken_a", drop_override_fields=("notice",)), _fixture_family("broken_b", drop_override_fields=("tags",))],
    )
    state_path = tmp_path / "application.json"
    monkeypatch.setattr(module, "_load", lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser layer must not be reached")))

    result = module.run_catalog_create(state_path, catalog_path)

    assert result["action"] == "all_pending_incomplete"
    assert {item["family"] for item in result["skipped"]} == {"broken_a", "broken_b"}
    assert module._read_catalog_listings(state_path) == {}


def test_real_catalog_now_selects_a_real_candidate_family_to_create(tmp_path):
    """Grounds slice 2 against slice 1's actual state: every real family's platform_overrides
    now carries a grounded subcategory (see test_listing_catalog.py's
    RealCatalogLancersOverrideGroundingTests), so today's real wake names a real family and a
    create-shaped product that passes _require_create_fields -- it no longer skips everything."""
    module = _module()
    state_path = tmp_path / "application.json"

    selection = module.select_catalog_family_to_create(module.DEFAULT_CATALOG, state_path)

    assert selection["action"] == "candidate_selected"
    assert isinstance(selection["family"], str) and selection["family"]
    module._require_create_fields(selection["product"])  # must not raise


# 9. When every family is published, the wake reports that and creates nothing -----------------


def test_select_reports_all_published_when_every_family_has_a_listing(tmp_path):
    module = _module()
    catalog_path = _write_fixture_catalog(tmp_path, [_fixture_family("alpha"), _fixture_family("beta")])
    state_path = tmp_path / "application.json"
    module._write_catalog_listing(state_path, "alpha", {"listing_external_id": "111111"})
    module._write_catalog_listing(state_path, "beta", {"listing_external_id": "222222"})

    selection = module.select_catalog_family_to_create(catalog_path, state_path)

    assert selection == {"action": "all_published", "skipped": []}


def test_run_catalog_create_never_touches_the_browser_layer_when_all_published(tmp_path, monkeypatch):
    module = _module()
    catalog_path = _write_fixture_catalog(tmp_path, [_fixture_family("alpha")])
    state_path = tmp_path / "application.json"
    module._write_catalog_listing(state_path, "alpha", {"listing_external_id": "111111"})
    monkeypatch.setattr(module, "_load", lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser layer must not be reached")))

    result = module.run_catalog_create(state_path, catalog_path)

    assert result == {"action": "all_published", "skipped": []}


# 10. --apply's behaviour on the existing listing is unchanged; catalogue creation is additive --


def test_main_apply_invokes_catalog_create_only_when_run_left_nothing_to_do(monkeypatch, tmp_path):
    module = _module()
    calls: list[Path] = []
    monkeypatch.setattr(module, "run", lambda apply, product_path, state_path: {"ok": True, "action": "unchanged"})
    monkeypatch.setattr(module, "run_catalog_create", lambda state_path: calls.append(state_path) or {"action": "all_published", "skipped": []})

    class _Delivery:
        delivery_uncertain = False
        pre_send_failed = False

    class _Reporter:
        @staticmethod
        def notify_storefront_wake(_result):
            return _Delivery()

    monkeypatch.setattr(module, "_load", lambda *_a, **_k: _Reporter())

    exit_code = module.main(["--apply"])

    assert exit_code == 0
    assert len(calls) == 1


def test_main_apply_does_not_invoke_catalog_create_when_run_already_had_an_effect(monkeypatch):
    module = _module()
    monkeypatch.setattr(module, "run", lambda apply, product_path, state_path: {"ok": True, "action": "updated"})
    monkeypatch.setattr(
        module, "run_catalog_create",
        lambda state_path: (_ for _ in ()).throw(AssertionError("run_catalog_create must not run when run() already had an effect")),
    )

    class _Delivery:
        delivery_uncertain = False
        pre_send_failed = False

    class _Reporter:
        @staticmethod
        def notify_storefront_wake(_result):
            return _Delivery()

    monkeypatch.setattr(module, "_load", lambda *_a, **_k: _Reporter())

    exit_code = module.main(["--apply"])

    assert exit_code == 0


def test_apply_flow_result_shape_for_the_existing_listing_is_unaffected_by_catalog_creation(monkeypatch):
    """--apply's own result dict for the existing single-offer listing keeps every field it had
    before; catalog_creation is purely additive, not a replacement of any existing key."""
    module = _module()
    existing_result = {"ok": True, "action": "unchanged", "aligned": True, "canonical_url": "https://www.lancers.jp/menu/detail/1338228"}
    monkeypatch.setattr(module, "run", lambda apply, product_path, state_path: dict(existing_result))
    monkeypatch.setattr(module, "run_catalog_create", lambda state_path: {"action": "all_published", "skipped": []})

    class _Delivery:
        delivery_uncertain = False
        pre_send_failed = False

    class _Reporter:
        @staticmethod
        def notify_storefront_wake(result):
            # Every pre-existing field survives untouched; only "catalog_creation" was added.
            for key, value in existing_result.items():
                assert result[key] == value
            assert set(result) == set(existing_result) | {"catalog_creation"}
            return _Delivery()

    monkeypatch.setattr(module, "_load", lambda *_a, **_k: _Reporter())

    exit_code = module.main(["--apply"])

    assert exit_code == 0
