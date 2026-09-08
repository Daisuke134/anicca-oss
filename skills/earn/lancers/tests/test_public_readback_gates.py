"""The wizard succeeded and could not tell -- two defects, both in _public()'s readback gate.

Thirteen wakes created skills/earn/lancers/scripts/storefront_offer.py's mvp_web_app_build
package live (https://www.lancers.jp/menu/detail/1342218, verified independently with no
login) and every one reported `publication_uncertain`, because:

1. _apply()'s two edit-success sites and create_package()'s own final readback each caught
   _public()'s specific OfferError and re-raised a bare "publication_uncertain" `from None`,
   discarding the underlying code (canonical_mismatch / contract_route_invalid / ...) and the
   traceback. A wake that hit this path had nothing to act on. Fixed by chaining the cause and
   naming it in the message: `raise OfferError(f"publication_uncertain: {error}") from error`.

2. _public() asserted two things every created listing genuinely cannot prove: Lancers' monthly
   -contract routes (basicMain/standardMain/premiumMain × 1/3/6 months -- a feature of the one
   hand-authored monthly service, monthly-sns-content-ops-v1.json, never set up by the manual
   creation wizard) and an attached image (画像ほか is an optional wizard step). Fixed by making
   both conditional on what the specific product/attempt actually claims:
   `product.get("sells_monthly_contract", False)` for the contract-route gate (a declared,
   validated field -- see _validate_product), and a `require_image` keyword _public() callers
   supply (create_package() passes the wizard's own `image_attached` flag; every pre-existing
   caller -- _apply(), run()'s --inspect path -- keeps the old default of True, so the one
   hand-authored listing's readback is exactly as strict as before this change).

Every other comparison _public() makes -- title, subtitle, description, notice, and every
plan's description/price/delivery_days -- is unconditional in every test below: this file
proves precisely which two assertions became conditional, and that nothing else did.

Run: python3 -m pytest skills/earn/lancers/tests/test_public_readback_gates.py
"""
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "skills/earn/lancers/scripts/storefront_offer.py"


def _module():
    spec = importlib.util.spec_from_file_location("storefront_offer_public_readback_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- A small, purpose-built fake of the public detail page (/menu/detail/<id>) ----------------
#
# Deliberately not the create-wizard fakes in test_create_package.py -- those model a six-step
# form, not a rendered detail page's canonical/og/plan-sidebar/monthly-contract-route/carousel
# markup, which is what _public() actually reads. Every field matches `product` by default; the
# mismatch_*/include_* knobs corrupt or omit exactly one thing at a time, so each test below
# proves exactly one assertion is (or is not) being made.

_ROUTE_ID = re.compile(r"(?:basic|standard|premium)Main[136]")


class _Attr:
    def __init__(self, *, text: str = "", attrs: dict[str, str] | None = None):
        self._text = text
        self._attrs = attrs or {}

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)


class _One:
    """A strict, single-match locator -- count()==1, forwards inner_text/get_attribute."""

    def __init__(self, elem: _Attr):
        self._elem = elem

    def count(self) -> int:
        return 1

    def inner_text(self) -> str:
        return self._elem.inner_text()

    def get_attribute(self, name: str) -> str | None:
        return self._elem.get_attribute(name)


class _None:
    def count(self) -> int:
        return 0


class _Many:
    def __init__(self, items: list):
        self._items = list(items)

    def all(self) -> list:
        return list(self._items)

    def count(self) -> int:
        return len(self._items)

    def nth(self, index: int):
        return self._items[index]


_PLAN_SECTION_SELECTORS = {
    "description": "p.p-menu-browse-detail__sidebar-description",
    "price": "div.p-menu-browse-detail__sidebar-header-price",
    "delivery": "div.p-menu-browse-detail__sidebar-menu",
}


class _PlanSection:
    def __init__(self, description: str, price_jpy: int, delivery_days: int):
        self._fields = {
            _PLAN_SECTION_SELECTORS["description"]: _One(_Attr(text=description)),
            _PLAN_SECTION_SELECTORS["price"]: _One(_Attr(text=f"{price_jpy}円")),
            _PLAN_SECTION_SELECTORS["delivery"]: _One(_Attr(text=f"納期{delivery_days}日")),
        }

    def drop_field(self, name: str) -> None:
        """Make one field's own selector resolve to zero matches -- models the
        counts != [1, 1, 1] branch of _public()'s plan-section read."""
        self._fields[_PLAN_SECTION_SELECTORS[name]] = _None()

    def blank_field(self, name: str) -> None:
        """Keep the field present (count == 1) but make its own parsed value empty -- models
        the "not description/not price/delivery is None" branch, which is reached only once
        every field's own count is already exactly 1."""
        self._fields[_PLAN_SECTION_SELECTORS[name]] = _One(_Attr(text=""))

    def locator(self, selector: str):
        return self._fields[selector]


class _PublicPage:
    """`include_monthly_routes=None` (the default) means the product does not claim monthly
    contracts and this fake asserts the #basicMain*/#standardMain*/#premiumMain* selectors are
    never even queried -- the proof that the gate is skipped, not merely satisfied by an absent
    element. Pass True/False only for a product that does claim them (sells_monthly_contract).
    """

    def __init__(
        self, product: dict, *, include_monthly_routes: bool | None = None, include_image: bool = True,
        mismatch_title: bool = False, mismatch_subtitle: bool = False, mismatch_description: bool = False,
        mismatch_notice: bool = False, mismatch_plan_price: bool = False,
        canonical_count: int = 1, canonical_href: str | None = "__default__",
        og_count: int = 1, og_content: str | None = "__default__",
        plan_missing_field: str | None = None, plan_empty_field: str | None = None,
        bad_route_selector: str | None = None, bad_route_value: str = "not-a-route",
    ):
        self._product = product
        self._include_monthly_routes = include_monthly_routes
        self._include_image = include_image
        self._mismatch_title = mismatch_title
        self._mismatch_subtitle = mismatch_subtitle
        self._mismatch_description = mismatch_description
        self._mismatch_notice = mismatch_notice
        self._mismatch_plan_price = mismatch_plan_price
        self._canonical_count = canonical_count
        self._canonical_href = canonical_href
        self._og_count = og_count
        self._og_content = og_content
        self._plan_missing_field = plan_missing_field  # one of "description"/"price"/"delivery"
        self._plan_empty_field = plan_empty_field  # one of "description"/"price"/"delivery"
        self._bad_route_selector = bad_route_selector
        self._bad_route_value = bad_route_value
        self.url: str | None = None
        self.goto_log: list[str] = []

    def _public_url(self) -> str:
        return f"https://www.lancers.jp/menu/detail/{self._product['listing_external_id']}"

    def goto(self, url: str, **_kwargs):
        self.goto_log.append(url)
        self.url = url
        return types.SimpleNamespace(status=200)

    def locator(self, selector: str):
        public_url = self._public_url()
        if selector == 'link[rel="canonical"]':
            if self._canonical_count != 1:
                return _Many([_Attr(attrs={"href": public_url}) for _ in range(self._canonical_count)])
            href = public_url if self._canonical_href == "__default__" else self._canonical_href
            return _One(_Attr(attrs={"href": href}))
        if selector == 'meta[property="og:url"]':
            if self._og_count != 1:
                return _Many([_Attr(attrs={"content": public_url}) for _ in range(self._og_count)])
            content = public_url if self._og_content == "__default__" else self._og_content
            return _One(_Attr(attrs={"content": content}))
        if selector == "li.p-menu-browse-detail__sidebar-content.js-project-plan-tab-content":
            sections = []
            for index, plan in enumerate(self._product["plans"]):
                price = plan["price_jpy"] + (1 if self._mismatch_plan_price and index == 0 else 0)
                section = _PlanSection(plan["description"], price, plan["delivery_days"])
                if index == 0 and self._plan_missing_field:
                    section.drop_field(self._plan_missing_field)
                if index == 0 and self._plan_empty_field:
                    section.blank_field(self._plan_empty_field)
                sections.append(section)
            return _Many(sections)
        if selector.startswith("#") and _ROUTE_ID.fullmatch(selector[1:]):
            if self._include_monthly_routes is None:
                raise AssertionError(
                    f"monthly-contract route selector queried for a product that does not "
                    f"claim sells_monthly_contract: {selector}"
                )
            if not self._include_monthly_routes:
                return _None()
            if selector == self._bad_route_selector:
                return _One(_Attr(attrs={"value": self._bad_route_value}))
            month = int(selector[-1])
            route = (
                "/project_board/quote_request?project_plan_menu_id=1" if month == 1
                else f"/monthly_work_contracts/client/abc/add?project_plan_menu_id=1&month={month}"
            )
            return _One(_Attr(attrs={"value": route}))
        if selector == ".p-menu-browse-detail__carousel-list img":
            if not self._include_image:
                return _Many([])
            return _Many([_Attr(attrs={"src": "https://img2.lancers.jp/projectblob/x.png"})])
        if selector == "h1":
            return _One(_Attr(text="MISMATCH" if self._mismatch_title else self._product["public_title"]))
        if selector == ".l-page-header__heading-description":
            return _One(_Attr(text="MISMATCH" if self._mismatch_subtitle else self._product["subtitle"]))
        if selector == "#body + .p-project-plan-markdown":
            return _One(_Attr(text="MISMATCH" if self._mismatch_description else self._product["description"]))
        if selector == "#notice_for_sale + .c-text":
            return _One(_Attr(text="MISMATCH" if self._mismatch_notice else self._product["notice"]))
        raise AssertionError(f"unexpected selector: {selector!r}")


def _base_product(**overrides) -> dict:
    product = {
        "listing_external_id": "999999",
        "public_title": "テストタイトルを開発します",
        "subtitle": "テストサブタイトル",
        "description": "テスト説明文です。",
        "notice": "テスト注意事項です。",
        "plans": [
            {"description": "ベーシック", "price_jpy": 100000, "delivery_days": 14},
            {"description": "スタンダード", "price_jpy": 200000, "delivery_days": 21},
            {"description": "プレミアム", "price_jpy": 300000, "delivery_days": 30},
        ],
    }
    product.update(overrides)
    return product


# 1. Baseline: a product that matches on every field is aligned, with every gate satisfied -----


def test_matching_product_without_monthly_claim_is_aligned():
    module = _module()
    product = _base_product()
    page = _PublicPage(product)

    result = module._public(page, product)

    assert result["ok"] is True
    assert result["aligned"] is True
    assert result["mismatched_fields"] == []


# 3. A product that does not claim monthly contracts passes without any basicMain* element -----


def test_product_without_monthly_claim_never_queries_contract_route_elements():
    module = _module()
    product = _base_product()  # no "sells_monthly_contract" key -> defaults False
    assert "sells_monthly_contract" not in product
    page = _PublicPage(product, include_monthly_routes=None)  # raises if queried at all

    result = module._public(page, product)

    assert result["aligned"] is True
    assert result["contract_routes"] is None


# 4. A product that does claim monthly contracts is still checked exactly as strictly -----------


def test_product_claiming_monthly_contracts_fails_when_routes_are_absent():
    module = _module()
    product = _base_product(sells_monthly_contract=True)
    page = _PublicPage(product, include_monthly_routes=False)

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    # Evidence-bearing now: names the selector it queried and what it actually found there.
    assert str(excinfo.value) == "contract_route_invalid: selector='#basicMain1' found=0"


def test_product_claiming_monthly_contracts_passes_when_routes_are_present():
    module = _module()
    product = _base_product(sells_monthly_contract=True)
    page = _PublicPage(product, include_monthly_routes=True)

    result = module._public(page, product)

    assert result["aligned"] is True
    assert result["contract_routes"] == {"spot": 3, "three_month": 3, "six_month": 3}


# 5/6. The image gate is conditional on what this specific attempt claims, via require_image ---


def test_missing_image_is_not_mismatched_when_caller_does_not_require_one():
    module = _module()
    product = _base_product()
    page = _PublicPage(product, include_image=False)

    result = module._public(page, product, require_image=False)

    assert result["aligned"] is True
    assert "image" not in result["mismatched_fields"]


def test_missing_image_is_mismatched_when_caller_requires_one():
    module = _module()
    product = _base_product()
    page = _PublicPage(product, include_image=False)

    result = module._public(page, product)  # require_image defaults True -- today's behaviour

    assert result["aligned"] is False
    assert result["mismatched_fields"] == ["image"]


def test_present_image_matches_when_caller_requires_one():
    module = _module()
    product = _base_product()
    page = _PublicPage(product, include_image=True)

    result = module._public(page, product, require_image=True)

    assert result["aligned"] is True
    assert "image" not in result["mismatched_fields"]


# 7. Title/subtitle/description/notice/plans stay mandatory for every caller -------------------
#
# Proven not vacuous per the task: temporarily dropping the "notice" comparison from _public()'s
# own `expected` dict makes test_mismatch_in_notice_still_fails below fail (no OfferError is
# raised); reverting makes it pass again -- confirmed manually while writing this file.


@pytest.mark.parametrize(
    ("kwarg", "expected_mismatched_key"),
    [
        ("mismatch_title", "title"),
        ("mismatch_subtitle", "subtitle"),
        ("mismatch_description", "description"),
        ("mismatch_notice", "notice"),
        ("mismatch_plan_price", "plans"),
    ],
)
def test_mismatch_in_each_mandatory_field_still_fails(kwarg, expected_mismatched_key):
    module = _module()
    product = _base_product()
    page = _PublicPage(product, **{kwarg: True})

    result = module._public(page, product)

    assert result["aligned"] is False
    assert expected_mismatched_key in result["mismatched_fields"]


def test_mismatch_in_notice_still_fails():
    """Named separately from the parametrized sweep above because this is the exact case the
    task's "prove test 7 is not vacuous" instruction was manually re-verified against."""
    module = _module()
    product = _base_product()
    page = _PublicPage(product, mismatch_notice=True)

    result = module._public(page, product)
    assert result["aligned"] is False
    assert result["mismatched_fields"] == ["notice"]


# --- Items 1/2: the readback failure at every re-raising call site carries its cause -----------
#
# _public() itself already raised a specific code before this task (public_readback_invalid,
# canonical_mismatch, contract_route_invalid, ...) -- the bug was three callers (_apply()'s two
# edit-success sites, create_package()'s own final readback) discarding it behind a bare
# "publication_uncertain" `from None`. create_package()'s own coverage lives in
# test_create_package.py (its wizard-walking fakes already exercise the full path); the two
# _apply() sites are covered here with minimal fakes scoped to exactly those two lines --
# everything else _apply() does (_field, _step, _select_service_type, _reconcile_superseded) is
# monkeypatched away since it is not what this task changed.


class _StubField:
    def fill(self, *_args, **_kwargs) -> None:
        pass

    def select_option(self, *_args, **_kwargs) -> None:
        pass

    def press(self, *_args, **_kwargs) -> None:
        pass


class _ExpectResponseCM:
    def __init__(self, status: int = 200):
        self.value = types.SimpleNamespace(status=status)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _UploadField:
    def evaluate(self, _script: str) -> bool:
        return True

    def set_input_files(self, _path: str) -> None:
        pass


class _UploadsLocator:
    def all(self) -> list:
        return [_UploadField()]


class _EmptyCountLocator:
    def count(self) -> int:
        return 0


class _SaveButton:
    def count(self) -> int:
        return 1

    def click(self, **_kwargs) -> None:
        pass


class _EditPage:
    """Just enough of the /myplan/<id>/edit page for _apply()'s full-update branch to reach its
    own final `_public()` call -- every field/step interaction is monkeypatched at the module
    level (see the tests below), so this fake only needs to satisfy the handful of calls _apply()
    makes directly against `page` itself."""

    def __init__(self):
        self.url: str | None = None

    def goto(self, url: str, **_kwargs):
        self.url = url
        return types.SimpleNamespace(status=200)

    def wait_for_selector(self, *_args, **_kwargs) -> None:
        pass

    def wait_for_function(self, *_args, **_kwargs) -> None:
        pass

    def wait_for_url(self, *_args, **_kwargs) -> None:
        pass

    def locator(self, selector: str):
        if selector == '[aria-label="削除"]':
            return _EmptyCountLocator()
        if selector == 'input[type="file"][accept*="image/"]':
            return _UploadsLocator()
        raise AssertionError(f"unexpected locator: {selector!r}")

    def expect_response(self, _predicate, timeout=None):
        return _ExpectResponseCM(200)

    def get_by_role(self, _role: str, *, name: str, exact: bool = True):
        assert name == "保存する"
        return _SaveButton()


def _edit_product() -> dict:
    return {
        "listing_external_id": "555555",
        "superseded_listing_ids": [],
        "title_stem": "新しいタイトルを開発し",
        "subtitle": "新しいサブタイトル",
        "category": "IT・プログラミング・開発",
        "subcategory": "システム開発（オーダーメイド）",
        "service_type": "Webアプリケーション構築",
        "industry": "IT・通信・インターネット",
        "tags": ["業務システム"],
        "plans": [
            {"description": "ライト", "price_jpy": 100000, "delivery_days": 14},
            {"description": "スタンダード", "price_jpy": 200000, "delivery_days": 21},
            {"description": "プレミアム", "price_jpy": 300000, "delivery_days": 30},
        ],
        "description": "新しい説明文です。",
        "notice": "新しい注意事項です。",
    }


def _patch_apply_internals(module, monkeypatch, *, public_results: list) -> None:
    calls = {"n": 0}

    def fake_public(_page, _product):
        calls["n"] += 1
        result = public_results[calls["n"] - 1]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(module, "_public", fake_public)
    monkeypatch.setattr(module, "_field", lambda *_a, **_k: _StubField())
    monkeypatch.setattr(module, "_step", lambda *_a, **_k: None)
    monkeypatch.setattr(module, "_select_service_type", lambda *_a, **_k: None)
    monkeypatch.setattr(
        module, "_reconcile_superseded",
        lambda *_a, **_k: {"superseded_visible_count": 0, "status_effect_count": 0},
    )


def test_apply_title_only_success_path_chains_the_public_readback_failure(monkeypatch):
    module = _module()
    product = _edit_product()
    _patch_apply_internals(
        module, monkeypatch,
        public_results=[
            {"aligned": False, "mismatched_fields": ["title"]},  # the "before" check
            module.OfferError("public_readback_invalid"),  # the post-save readback
        ],
    )
    page = _EditPage()

    with pytest.raises(module.OfferError) as excinfo:
        module._apply(page, product, Path("/tmp/irrelevant.png"))

    assert str(excinfo.value) == "publication_uncertain: public_readback_invalid"
    assert isinstance(excinfo.value.__cause__, module.OfferError)
    assert str(excinfo.value.__cause__) == "public_readback_invalid"


def test_apply_full_update_success_path_chains_the_public_readback_failure(monkeypatch):
    module = _module()
    product = _edit_product()
    _patch_apply_internals(
        module, monkeypatch,
        public_results=[
            {"aligned": False, "mismatched_fields": ["subtitle", "description"]},  # "before"
            module.OfferError("canonical_mismatch"),  # the post-save readback
        ],
    )
    page = _EditPage()

    with pytest.raises(module.OfferError) as excinfo:
        module._apply(page, product, Path("/tmp/irrelevant.png"))

    assert str(excinfo.value) == "publication_uncertain: canonical_mismatch"
    assert isinstance(excinfo.value.__cause__, module.OfferError)
    assert str(excinfo.value.__cause__) == "canonical_mismatch"


# --- Items 5-8: every _public()/_text() refusal now names what it observed, not only its own ---
# bare code. Every check itself is byte-for-byte the same comparison as before -- only the
# OfferError message a failing comparison raises gained evidence; test_matching_product_without_
# monthly_claim_is_aligned and test_product_claiming_monthly_contracts_passes_when_routes_are_
# present (above) already prove a genuinely matching page still passes every one of these checks,
# so each test below only needs to prove the mismatched case still raises, now with evidence.


# 5. canonical_mismatch names the counts and both observed values -------------------------------


def test_canonical_mismatch_names_counts_and_both_observed_values_and_page_url():
    module = _module()
    product = _base_product()
    wrong_href = "https://www.lancers.jp/menu/detail/000000"
    page = _PublicPage(product, canonical_href=wrong_href)

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    message = str(excinfo.value)
    expected_url = page._public_url()
    assert message.startswith("canonical_mismatch: ")
    assert "canonical_count=1" in message
    assert f"canonical_href={wrong_href!r}" in message
    assert "og_count=1" in message
    assert f"og_content={expected_url!r}" in message
    assert f"expected={expected_url!r}" in message
    assert f"page_url={expected_url!r}" in message


def test_canonical_mismatch_never_reads_the_attribute_of_a_missing_canonical_element():
    """count()!=1 must short-circuit the attribute read -- a real Playwright locator raises its
    own strict-mode error if get_attribute() is called against zero or several matches, so the
    evidence for a missing element must come from count() alone, never from calling
    get_attribute() on it."""
    module = _module()
    product = _base_product()
    page = _PublicPage(product, canonical_count=0)

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    message = str(excinfo.value)
    assert "canonical_count=0" in message
    assert "canonical_href=None" in message


def test_canonical_mismatch_names_the_og_url_when_only_it_disagrees():
    module = _module()
    product = _base_product()
    wrong_content = "https://www.lancers.jp/menu/detail/000000"
    page = _PublicPage(product, og_content=wrong_content)

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    message = str(excinfo.value)
    assert f"canonical_href={page._public_url()!r}" in message  # canonical itself was fine
    assert f"og_content={wrong_content!r}" in message


# 6. Each distinct public_readback_invalid condition names which one failed and the value --------


def test_text_readback_names_the_missing_selector_and_field():
    module = _module()
    page = types.SimpleNamespace(locator=lambda _selector: _None())

    with pytest.raises(module.OfferError) as excinfo:
        module._text(page, "h1", field="title")

    assert str(excinfo.value) == "public_readback_invalid: field='title' selector='h1' found=0"


def test_text_readback_names_the_field_when_the_selector_matches_but_is_blank():
    module = _module()
    page = types.SimpleNamespace(locator=lambda _selector: _One(_Attr(text="   ")))

    with pytest.raises(module.OfferError) as excinfo:
        module._text(page, "h1", field="title")

    assert str(excinfo.value) == "public_readback_invalid: field='title' selector='h1' text=''"


def test_plan_section_field_count_mismatch_names_the_selectors_and_counts():
    module = _module()
    product = _base_product()
    page = _PublicPage(product, plan_missing_field="price")

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    message = str(excinfo.value)
    assert message.startswith("public_readback_invalid: field='plan_section' plan_index=0 ")
    assert "found=[1, 0, 1]" in message


def test_plan_section_blank_field_names_which_field_and_the_raw_value():
    module = _module()
    product = _base_product()
    page = _PublicPage(product, plan_empty_field="delivery")

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    message = str(excinfo.value)
    assert message.startswith("public_readback_invalid: field='delivery_days' plan_index=0 ")
    assert "raw=''" in message


def test_page_level_readback_names_the_expected_and_observed_url():
    module = _module()
    product = _base_product()
    page = _PublicPage(product)
    real_goto = page.goto

    def _bouncing_goto(url, **kwargs):
        response = real_goto(url, **kwargs)
        page.url = "https://www.lancers.jp/login"  # models a session bounce mid-navigation
        return response

    page.goto = _bouncing_goto

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    message = str(excinfo.value)
    assert message.startswith("public_readback_invalid: field='page' ")
    assert f"expected_url={page._public_url()!r}" in message
    assert "page_url='https://www.lancers.jp/login'" in message


# 7. contract_route_invalid names the selector and the observed route ---------------------------


def test_contract_route_invalid_names_the_selector_when_the_element_is_missing():
    """Named separately from test_product_claiming_monthly_contracts_fails_when_routes_are_absent
    above only to keep this file's item-7 coverage in one place; that test already asserts the
    exact found=0 message this one restates."""
    module = _module()
    product = _base_product(sells_monthly_contract=True)
    page = _PublicPage(product, include_monthly_routes=False)

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    assert str(excinfo.value) == "contract_route_invalid: selector='#basicMain1' found=0"


def test_contract_route_invalid_names_the_selector_and_the_observed_route_on_a_pattern_mismatch():
    module = _module()
    product = _base_product(sells_monthly_contract=True)
    page = _PublicPage(product, include_monthly_routes=True, bad_route_selector="#basicMain1", bad_route_value="/not/a/real/route")

    with pytest.raises(module.OfferError) as excinfo:
        module._public(page, product)

    assert str(excinfo.value) == (
        "contract_route_invalid: selector='#basicMain1' route='/not/a/real/route' "
        "expected_pattern='/project_board/quote_request\\\\?project_plan_menu_id=[0-9]+'"
    )


# 8. Every check stays exactly as strict -- proven by the two combined facts: every mismatch test
# above (and the pre-existing mismatch/contract-route/image tests earlier in this file) still
# raises, and test_matching_product_without_monthly_claim_is_aligned /
# test_product_claiming_monthly_contracts_passes_when_routes_are_present prove the identical
# genuinely-correct page still passes every one of them. Nothing became looser; only the
# messages a real failure raises gained evidence.
