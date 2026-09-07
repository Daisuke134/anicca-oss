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
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "skills/earn/lancers/scripts/storefront_offer.py"


def _module():
    spec = importlib.util.spec_from_file_location("storefront_offer_create_package_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- fakes ------------------------------------------------------------------------------------
#
# Wizard step indices (mirroring the live stepper's own order): 0=基本情報, 1=料金表,
# 2=業務内容, 3=確認事項, 4=画像ほか, 5=公開.

# The 16 delivery_time options the live DOM read carried: a blank placeholder plus one label per
# day count Lancers' project_lancers()/LANCERS_DELIVERY_DAYS actually offers.
_DELIVERY_DAYS = (1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 30, 45, 60, 75, 90)
_IMAGE_STEP_MARKER_TEXT = "受注率が約10倍になります"  # mirrors storefront_offer._CREATE_IMAGE_STEP_MARKER_TEXT


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
    """

    def __init__(self, *, options: list[_Option] | None = None, visible: bool = True, text: str = "", step: int | None = None, name: str = "", attrs: dict[str, str] | None = None):
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

    def locator(self, selector: str) -> _OptionList:
        if selector == "option":
            return _OptionList(self._options)
        if selector == "option:checked":
            if self._options and self._selected_index is not None:
                return _OptionList([self._options[self._selected_index]])
            return _OptionList([])
        # A non-select field (e.g. a text input) queried for "option" -- 0 results, exactly like
        # a real Playwright locator finding no descendant <option> elements.
        return _OptionList([])

    def wait_for(self, state: str = "visible", timeout=None) -> None:
        if state != "visible":
            raise NotImplementedError(state)
        if not self.is_visible():
            raise TimeoutError(f"field {self._name!r} not visible (step={self._step})")


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
    ):
        self.url = "https://www.lancers.jp/myplan"
        self.goto_log: list[str] = []
        self.event_log: list[tuple[str, str, int]] = []
        self._fields = fields or {}
        for field in self._fields.values():
            field.page = self
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

        self._image_marker = _Field(step=4, text=_IMAGE_STEP_MARKER_TEXT, name="画像ほかマーカー")
        self._image_marker.page = self

    def goto(self, url: str, **_kwargs) -> _Response:
        self.goto_log.append(url)
        self.url = url
        return _Response(200)

    def locator(self, selector: str):
        if selector == "button":
            return _LocatorList(self._buttons)
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
        return self._fields.get(selector, _EmptyField())

    def get_by_text(self, label: str, exact: bool = True):
        if label == "手動でパッケージを作成する":
            return _LocatorList([self._manual_button])
        if label == "次へ":
            return _LocatorList([self._next_button])
        if label == _IMAGE_STEP_MARKER_TEXT and not exact:
            return _LocatorList([self._image_marker])
        return _LocatorList([])

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        pass

    def wait_for_function(self, *_args, **_kwargs) -> None:
        pass

    def wait_for_url(self, pattern, timeout=None) -> None:
        if self._after_submit_url is not None:
            self.url = self._after_submit_url


def _complete_product(**overrides) -> dict:
    product = {
        "title_stem": "業務システムを開発し",
        "subtitle": "小規模チーム向けの業務システムを開発します",
        "category": "IT・プログラミング・開発",
        "subcategory": "システム開発（オーダーメイド）",
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


def _fields_for(product: dict) -> dict[str, _Field]:
    fields = {
        '[name="ProjectPlanForm.title"]': _Field(step=0, name="title"),
        '[name="ProjectPlanForm.subtitle"]': _Field(step=0, name="subtitle"),
        '[name="___main_category_id"]': _Field(options=_select_options_for(product["category"]), step=0, name="category"),
        '[name="ProjectPlanForm.project_category_id"]': _Field(options=_select_options_for(product["subcategory"]), step=0, name="subcategory"),
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
    assert result == {"image_attached": True}
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
    assert by_name["title"] == {"field": "title", "present": True, "type": "text", "filled": True}
    assert by_name["subtitle"] == {"field": "subtitle", "present": True, "type": "text", "filled": False}


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

    assert payload["advance_control"] == {"found": True, "text": "次へ", "disabled": False}


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
    assert payload["advance_control"] == {"found": True, "text": "次へ", "disabled": False}
    by_name = {item["field"]: item for item in payload["fields"]}
    assert set(by_name) == {"title", "subtitle", "category", "subcategory", "industry", "tags"}
    # Every 基本情報 field was already filled before the stalled advance was even attempted.
    assert by_name["title"]["filled"] is True
    assert by_name["category"]["selected_label"] == product["category"]
    assert "tag_widget" in payload


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

    assert result == {"image_attached": False}
    assert page.current_step == 5  # the wizard still reached 公開


def test_file_upload_failure_yields_image_attached_false_without_raising():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None, file_input_count=4, file_upload_raises=True)

    result = module._fill_create_form(page, product, Path("/tmp/irrelevant.png"))

    assert result == {"image_attached": False}


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

    assert str(excinfo.value) == "publication_uncertain"
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
        "industry": "IT・通信・インターネット",
        "tags": [family],
        "notice": f"{family}のご相談内容を確認してから進めます。",
    }
    if extra_override: override.update(extra_override)
    for field in drop_override_fields: override.pop(field, None)
    return {
        "id": family.replace("_", "-"),
        "family": family,
        "title_ja": f"{family}を開発します",
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
