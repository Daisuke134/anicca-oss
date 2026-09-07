"""_field() used to say only "form_changed" -- discarding the selector it was matching, how many
elements matched, and what page it was looking at. Measured 2026-09-08: a wake logged
`"error":"form_changed"` with no `label=`/`found=` suffix and no dom-contract-failures.jsonl row
at all, even though _step() (its sibling helper right next to it in this file) already carried
that evidence after an earlier fix. Grepping the running release found the reason: _field()
never got the same fix, so every one of its call sites -- _apply(), the profile flow, the
settings flow, the portfolio flow, and _fill_create_form() -- still raised the same anonymous
"form_changed" _step() used to.

This tests _field()'s new refusal path -- routed through
skills/_shared/marketplace-core/scripts/dom_contract.py's `exactly_one`, mirroring _step()'s own
fix exactly -- without touching what counts as a match: still exactly one element, still a bare
"form_changed" for every existing (non-create) call site (context is opt-in), still no locator
returned on an ambiguous or absent match.

Run: python3 -m pytest skills/earn/lancers/tests/test_field_says_which_selector.py
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
    spec = importlib.util.spec_from_file_location("storefront_offer_field_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dom_contract():
    spec = importlib.util.spec_from_file_location("dom_contract_for_field_test", DOM_CONTRACT)
    dom = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = dom
    spec.loader.exec_module(dom)
    return dom


class _Locator:
    """A minimal stand-in for what `page.locator(selector)` resolves to: only `.count()` is
    exercised by `_field()`/`dom_contract.exactly_one`, exactly as production only ever calls
    `.count()` before deciding whether to hand the locator back."""

    def __init__(self, count: int) -> None:
        self._count = count

    def count(self) -> int:
        return self._count


class _Page:
    """A minimal stand-in for the Playwright page _field() drives: locator() resolves a selector
    to whatever count the test wired up; url/title are what _page_identity() reads."""

    def __init__(self, counts: dict[str, int], *, url: str, title: str | None = "パッケージ作成 | Lancers") -> None:
        self._counts = counts
        self.url = url
        self._title = title

    def locator(self, selector: str) -> _Locator:
        return _Locator(self._counts.get(selector, 0))

    def title(self) -> str | None:
        return self._title


def _evidence_dir(module, tmp_path: Path) -> Path:
    directory = tmp_path / "evidence"
    module._EVIDENCE_DIR = directory
    return directory


# 1. Zero matches raises with the selector and count 0 in the evidence --------------------------


def test_zero_matches_names_the_selector_and_count_in_the_evidence(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    page = _Page({}, url="https://www.lancers.jp/myplan/add?type=manual")

    with pytest.raises(module.OfferError) as excinfo:
        module._field(page, '[name="ProjectPlanForm.title"]')

    message = str(excinfo.value)
    assert "selector='[name=\"ProjectPlanForm.title\"]'" in message
    assert "found=0" in message
    dom = _dom_contract()
    rows = dom.failures(directory)
    assert len(rows) == 1
    assert rows[0]["selector"] == '[name="ProjectPlanForm.title"]'
    assert rows[0]["found"] == 0
    assert rows[0]["platform"] == "lancers"


# 2. Two matches raises naming count 2, and does not return a locator ---------------------------


def test_two_matches_names_the_count_and_returns_nothing(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    page = _Page({"#UserProfileSubTitle": 2}, url="https://www.lancers.jp/mypage/profile")

    with pytest.raises(module.OfferError) as excinfo:
        module._field(page, "#UserProfileSubTitle")

    assert "found=2" in str(excinfo.value)
    dom = _dom_contract()
    assert dom.failures(directory)[0]["found"] == 2


# 3. The evidence carries page identity, not only the selector ----------------------------------


def test_evidence_carries_page_identity_not_only_the_selector(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    page = _Page({}, url="https://www.lancers.jp/login", title="ログイン | Lancers")

    with pytest.raises(module.OfferError):
        module._field(page, '[name="ProjectPlanForm.subtitle"]')

    dom = _dom_contract()
    observed = dom.failures(directory)[0]["observed"]
    assert observed["url"] == "https://www.lancers.jp/login"
    assert observed["title"] == "ログイン | Lancers"


# 4. A create-path failure is distinguishable from a bare (_apply()-style) failure --------------


def test_create_path_context_is_distinguishable_from_a_bare_failure(tmp_path):
    module = _module()
    _evidence_dir(module, tmp_path)
    page = _Page({}, url=module.ORIGIN + "/myplan/add?type=manual")

    with pytest.raises(module.OfferError) as with_context:
        module._field(page, '[name="ProjectPlanForm.title"]', context="create:title")
    with pytest.raises(module.OfferError) as bare:
        module._field(page, '[name="ProjectPlanForm.title"]')

    assert "create:title" in str(with_context.value)
    assert "create:title" not in str(bare.value)
    assert str(with_context.value) != str(bare.value)
    assert str(bare.value).startswith('form_changed: selector=')


def test_field_source_no_longer_raises_a_bare_form_changed():
    """Prove test 1 is not vacuous: a source check catching the exact regression a careless
    future edit could reintroduce, the same convention test_step_says_what_it_saw.py already
    uses for _step(). Manually verified per the task's own instruction: reverting _field() to
    `if field.count() != 1: raise OfferError("form_changed")` makes
    test_zero_matches_names_the_selector_and_count_in_the_evidence fail (no `selector=`/`found=`
    in the message, no evidence row written), and this source check fails too; restoring the fix
    makes both pass again."""
    source = SCRIPT.read_text(encoding="utf-8")
    field_source = source[source.index("def _field("):source.index("def _setting_status(")]
    assert 'raise OfferError("form_changed")' not in field_source
    assert "found={error.found}" in field_source


# 5. Each _fill_create_form field failure names that specific field -----------------------------


def test_first_create_form_field_failure_names_its_own_field(tmp_path):
    """_fill_create_form()'s very first call is the title field -- ambiguous here, it must raise
    before anything else in the wizard runs (no wait_for_function, no later field), naming
    'create:title' rather than a bare form_changed."""
    module = _module()
    _evidence_dir(module, tmp_path)

    class _AbortingPage(_Page):
        def wait_for_function(self, *_args, **_kwargs):
            raise AssertionError("must not reach wait_for_function: title field must fail first")

    page = _AbortingPage(
        {'[name="ProjectPlanForm.title"]': 2}, url=module.ORIGIN + "/myplan/add?type=manual",
    )
    product = {"title_stem": "x" * 30}

    with pytest.raises(module.OfferError) as excinfo:
        module._fill_create_form(page, product, Path("/tmp/does-not-matter.jpg"))

    assert "create:title" in str(excinfo.value)
    assert "found=2" in str(excinfo.value)


def test_every_fill_create_form_field_carries_a_distinct_context():
    """Every _field()/_select_create_service_type()/_select_delivery_time() call inside
    _fill_create_form() must pass its own context, and no two of them collide -- otherwise two
    different broken fields would raise the same message and a wake still could not tell which
    one to fix."""
    source = SCRIPT.read_text(encoding="utf-8")
    form_source = source[source.index("def _fill_create_form("):source.index("def _create_submit_control(")]
    contexts = re.findall(r'context=(?:"(create:[^"]*)"|f"(create:[^"]*)")', form_source)
    flattened = [literal or template for literal, template in contexts]
    assert flattened == [
        "create:title", "create:subtitle", "create:category", "create:subcategory",
        "create:service_type", "create:industry_type", "create:tags",
        "create:plan[{index}].description", "create:plan[{index}].delivery_time",
        "create:plan[{index}].price", "create:notice_for_sale",
    ]
    assert len(set(flattened)) == len(flattened)


# 6. Existing non-create call sites are unchanged bare calls and behave identically -------------


def test_apply_profile_settings_and_portfolio_call_sites_are_unmodified_bare_field_calls():
    """Every _field() call inside _apply(), _profile()'s save path, _setting_status()/
    _reconcile_superseded(), and the portfolio flow must still be the same one-positional-
    argument shape it always was -- no context, so its default (bare "form_changed"-shaped)
    behaviour is exactly what it was before this change."""
    source = SCRIPT.read_text(encoding="utf-8")
    apply_source = source[source.index("def _apply("):source.index("# --- Package creation")]
    assert "context=" not in apply_source
    assert apply_source.count("_field(page, ") == 12  # unchanged from before this fix

    portfolio_source = source[source.index("def _ensure_portfolio("):source.index("def ensure_profile(")]
    assert "context=" not in portfolio_source
    assert portfolio_source.count("_field(page, ") == 6  # unchanged from before this fix

    profile_source = source[source.index("def _profile("):source.index("def _field(")]
    assert "context=" not in profile_source
    assert profile_source.count("_field(page, ") == 3  # unchanged from before this fix

    settings_source = source[source.index("def _setting_status("):source.index("def _write_receipt(")]
    assert "context=" not in settings_source
    assert settings_source.count("_field(page, ") == 1  # unchanged from before this fix


def test_apply_style_call_with_no_context_still_fails_closed_on_ambiguity(tmp_path):
    """Exercises _field() exactly as _apply()'s own bare calls do: two matches for a selector
    that should be unique must still refuse rather than hand back an ambiguous locator, with no
    context and the same bare "form_changed" prefix _apply() has always raised."""
    module = _module()
    _evidence_dir(module, tmp_path)
    page = _Page({'[name="ProjectPlanForm.title"]': 2}, url="https://www.lancers.jp/myplan/12345/edit")

    with pytest.raises(module.OfferError) as excinfo:
        module._field(page, '[name="ProjectPlanForm.title"]')

    assert str(excinfo.value).startswith('form_changed: selector=\'[name="ProjectPlanForm.title"]\'')


# 7. A clean single match still returns the locator ----------------------------------------------


def test_a_clean_single_match_still_returns_the_locator(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    page = _Page({'[name="ProjectPlanForm.title"]': 1}, url="https://www.lancers.jp/myplan/12345/edit")

    result = module._field(page, '[name="ProjectPlanForm.title"]')

    assert result.count() == 1
    dom = _dom_contract()
    assert dom.failures(directory) == []


def test_a_clean_single_match_with_context_also_returns_the_locator_and_records_no_evidence(tmp_path):
    module = _module()
    directory = _evidence_dir(module, tmp_path)
    page = _Page({'[name="ProjectPlanForm.title"]': 1}, url="https://www.lancers.jp/myplan/add?type=manual")

    result = module._field(page, '[name="ProjectPlanForm.title"]', context="create:title")

    assert result.count() == 1
    dom = _dom_contract()
    assert dom.failures(directory) == []
