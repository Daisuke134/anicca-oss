"""create_package() -- the manual-creation counterpart to _apply().

_apply() can only edit a package that already carries a listing_external_id: it goes straight
to /myplan/{listing_id}/edit. There was no path from the shared catalogue (twenty families,
now all projecting to legal Lancers plans) to a brand-new Lancers package -- one hand-authored
listing was all this lane could ever produce. create_package() is that path, reached by
clicking the manual option at /myplan/add -> /myplan/add?type=manual.

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

# The 16 delivery_time options the live DOM read carried: a blank placeholder plus one label per
# day count Lancers' project_lancers()/LANCERS_DELIVERY_DAYS actually offers.
_DELIVERY_DAYS = (1, 2, 3, 4, 5, 6, 7, 10, 14, 21, 30, 45, 60, 75, 90)


class _Option:
    def __init__(self, label: str, value: str):
        self._label = label
        self._value = value

    def inner_text(self) -> str:
        return self._label

    def get_attribute(self, name: str) -> str | None:
        return self._value if name == "value" else None


class _OptionList:
    def __init__(self, options: list[_Option]):
        self._options = options

    def all(self) -> list[_Option]:
        return list(self._options)


def _delivery_options() -> list[_Option]:
    options = [_Option("選択してください", "")]
    options.extend(_Option(f"{days}日", str(days)) for days in _DELIVERY_DAYS)
    return options


class _Field:
    """One form control. Records every fill/select_option/press call it receives."""

    def __init__(self, *, options: list[_Option] | None = None, visible: bool = True, text: str = ""):
        self.fills: list[str] = []
        self.selected: list[dict] = []
        self.presses: list[str] = []
        self.clicks = 0
        self._options = options or []
        self._visible = visible
        self._text = text

    def count(self) -> int:
        return 1

    def fill(self, value: str) -> None:
        self.fills.append(value)

    def select_option(self, *_args, **kwargs) -> None:
        self.selected.append(kwargs)

    def press(self, key: str) -> None:
        self.presses.append(key)

    def is_visible(self) -> bool:
        return self._visible

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, _name: str) -> str | None:
        return None

    def click(self, **_kwargs) -> None:
        self.clicks += 1

    def locator(self, selector: str) -> _OptionList:
        assert selector == "option"
        return _OptionList(self._options)


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


class _Response:
    def __init__(self, status: int = 200):
        self.status = status


class _FakeCreatePage:
    """A minimal stand-in for the Playwright page create_package() drives.

    `fields` maps an exact selector to a `_Field`. `buttons` (for `page.locator("button")`) and
    `by_text` (for `page.get_by_text(label, exact=True)`, which `_step()` uses to find the
    manual-creation button) are configured per test. `manual_button_lands_on` controls what
    clicking the manual button does to `page.url` -- the production equivalent of a real
    navigation completing.
    """

    def __init__(
        self,
        *,
        fields: dict[str, _Field] | None = None,
        buttons: list[_Field] | None = None,
        manual_button_lands_on: str | None,
        after_submit_url: str | None = None,
    ):
        self.url = "https://www.lancers.jp/myplan"
        self.goto_log: list[str] = []
        self._fields = fields or {}
        self._buttons = buttons if buttons is not None else []
        self._manual_button_lands_on = manual_button_lands_on
        self._after_submit_url = after_submit_url

        def _click_manual() -> None:
            if self._manual_button_lands_on is not None:
                self.url = self._manual_button_lands_on

        self._manual_button = _Field(text="手動でパッケージを作成する")
        self._manual_button.click = lambda **_kwargs: (_click_manual(), setattr(self._manual_button, "clicks", self._manual_button.clicks + 1))[-1]

    def goto(self, url: str, **_kwargs) -> _Response:
        self.goto_log.append(url)
        self.url = url
        return _Response(200)

    def locator(self, selector: str):
        if selector == "button":
            return _OptionList(self._buttons)
        return self._fields.get(selector, _EmptyField())

    def get_by_text(self, label: str, exact: bool = True):
        if label == "手動でパッケージを作成する":
            return _OptionList([self._manual_button])
        return _OptionList([])

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
        "plans": [
            {"description": "ライトプラン", "price_jpy": 100000, "delivery_days": 14},
            {"description": "スタンダードプラン", "price_jpy": 200000, "delivery_days": 21},
            {"description": "プレミアムプラン", "price_jpy": 300000, "delivery_days": 30},
        ],
    }
    product.update(overrides)
    return product


def _fields_for(product: dict) -> dict[str, _Field]:
    fields = {
        '[name="ProjectPlanForm.title"]': _Field(),
        '[name="ProjectPlanForm.subtitle"]': _Field(),
        '[name="___main_category_id"]': _Field(),
        '[name="ProjectPlanForm.project_category_id"]': _Field(),
        '[name="ProjectPlanForm.industry_type_id"]': _Field(),
        '[name="MultiSelectTagSearch_ProjectPlanTagForm"]': _Field(),
        '[name="ProjectPlanForm.notice_for_sale"]': _Field(),
    }
    for index, _plan in enumerate(product["plans"]):
        prefix = f"ProjectPlanMenuForm[{index}]"
        fields[f'[name="{prefix}.description"]'] = _Field()
        fields[f'[name="{prefix}.delivery_time"]'] = _Field(options=_delivery_options())
        fields[f'[name="{prefix}.price"]'] = _Field()
    return fields


# 1. A complete product produces one fill per observed field name, plans in order 0/1/2 --------


def test_complete_product_fills_every_observed_field_exactly_once():
    module = _module()
    product = _complete_product()
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    module._fill_create_form(page, product)

    assert fields['[name="ProjectPlanForm.title"]'].fills == [product["title_stem"]]
    assert fields['[name="ProjectPlanForm.subtitle"]'].fills == [product["subtitle"]]
    assert fields['[name="___main_category_id"]'].selected == [{"label": product["category"]}]
    assert fields['[name="ProjectPlanForm.project_category_id"]'].selected == [{"label": product["subcategory"]}]
    assert fields['[name="ProjectPlanForm.industry_type_id"]'].selected == [{"label": product["industry"]}]
    assert fields['[name="MultiSelectTagSearch_ProjectPlanTagForm"]'].fills == product["tags"]
    assert fields['[name="ProjectPlanForm.notice_for_sale"]'].fills == [product["notice"]]

    for index, plan in enumerate(product["plans"]):
        prefix = f"ProjectPlanMenuForm[{index}]"
        assert fields[f'[name="{prefix}.description"]'].fills == [plan["description"]]
        assert fields[f'[name="{prefix}.price"]'].fills == [str(plan["price_jpy"])]
        selected = fields[f'[name="{prefix}.delivery_time"]'].selected
        assert selected == [{"value": str(plan["delivery_days"])}]


# 2. A delivery_days with no matching option raises, naming the value and options; nothing selected


def test_unmatched_delivery_days_raises_and_selects_nothing():
    module = _module()
    product = _complete_product()
    product["plans"][1]["delivery_days"] = 18  # not in LANCERS_DELIVERY_DAYS / the options seen
    fields = _fields_for(product)
    page = _FakeCreatePage(fields=fields, manual_button_lands_on=None)

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product)

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
    # The public page was actually visited (as _public() always does) before giving up.
    assert page.goto_log[-1] == module.ORIGIN + "/menu/detail/999999"


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
