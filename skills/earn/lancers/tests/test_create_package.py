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


# --- Catalogue-driven creation: select_catalog_family_to_create / run_catalog_create ----------
#
# The wake now has a decision, not just a capability: after the existing align/inspect chain in
# run() leaves nothing to do, pick one catalogue family with no live Lancers listing and create
# it. select_catalog_family_to_create() is pure (no browser); run_catalog_create() is the one
# browser-touching wrapper main() reaches for. These fixtures build a small three-family
# catalogue rather than depending on the real twenty-family one, so "a family is creatable" and
# "a family is not" can both be exercised directly -- the real catalogue's own grounding (every
# family's category/industry/tags/notice, and that none carries a subcategory) is covered
# separately in skills/_shared/marketplace-core/tests/test_listing_catalog.py.


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


def test_real_catalog_is_currently_all_pending_incomplete_because_subcategory_is_unobserved(tmp_path):
    """Grounds slice 2 against slice 1's actual state: every real family's platform_overrides
    intentionally omits subcategory (see test_listing_catalog.py's
    test_no_family_carries_a_subcategory_override), so today's real wake names all twenty
    families under "skipped" and creates nothing -- it does not silently invent a value."""
    module = _module()
    state_path = tmp_path / "application.json"

    selection = module.select_catalog_family_to_create(module.DEFAULT_CATALOG, state_path)

    assert selection["action"] == "all_pending_incomplete"
    assert len(selection["skipped"]) == 20
    assert all(item["reason"] == "create_field_missing: subcategory" for item in selection["skipped"])


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
