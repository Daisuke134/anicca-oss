from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[4]
PATH = ROOT / "skills/earn/crowdworks/scripts/application_tick.py"

PROJECT_ID = "13423472"
AMOUNT_MINOR = 30000
DELIVERY_DUE_ON = "2026-09-10"
PROPOSAL_TEXT = "対応いたします。"
EXPIRE_PERIOD_DAYS = 7


def load():
    spec = importlib.util.spec_from_file_location(
        "crowdworks_application_tick_uncertainty_test", PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Fields:
    """A flat selector -> element registry, mirroring how test_application_loop_hol.py fakes a
    Playwright page (one dict of locators keyed by selector, independent of which URL the fake
    page currently reports)."""

    def __init__(self):
        self._store: dict[str, "_Field"] = {}

    def set(self, selector: str, field: "_Field") -> "_Field":
        self._store[selector] = field
        return field

    def locator(self, selector: str) -> "_Field":
        return self._store.get(selector, _Field(self, count=0))


class _Field:
    """The small slice of the Playwright Locator surface application_tick.py calls."""

    def __init__(self, fields: _Fields, *, count: int = 1, attrs=None, value: str = "", enabled: bool = True, items=None):
        self._fields = fields
        self._count = count
        self._attrs = dict(attrs or {})
        self._value = value
        self._enabled = enabled
        self._items = items or []

    def count(self) -> int:
        return self._count

    def nth(self, index: int) -> "_Field":
        return self._items[index]

    def get_attribute(self, name: str):
        return self._attrs.get(name)

    def input_value(self) -> str:
        return self._value

    def inner_text(self) -> str:
        return self._value

    def fill(self, value: str) -> None:
        self._value = value

    def blur(self) -> None:
        pass

    def check(self) -> None:
        pass

    def is_enabled(self) -> bool:
        return self._enabled

    def select_option(self, value: str) -> None:
        self._value = value

    def click(self, no_wait_after: bool = False) -> None:
        pass

    def locator(self, selector: str) -> "_Field":
        return self._fields.locator(selector)


class _Page:
    """A minimal Playwright Page stand-in exposing only what application_tick.py touches."""

    def __init__(self, fields: _Fields, *, url: str, wait_for_url=None, goto_errors=None):
        self._fields = fields
        self.url = url
        self.goto_log: list[str] = []
        self._goto_errors = goto_errors or {}
        if wait_for_url is not None:
            self.wait_for_url = wait_for_url

    def goto(self, url: str) -> None:
        self.goto_log.append(url)
        if url in self._goto_errors:
            raise self._goto_errors[url]
        self.url = url

    def locator(self, selector: str) -> _Field:
        return self._fields.locator(selector)

    def wait_for_timeout(self, ms: int) -> None:
        pass


def _due_line(delivery_due_on: str) -> str:
    year, month, day = (int(part) for part in delivery_due_on.split("-"))
    weekday = "月火水木金土日"[date(year, month, day).weekday()]
    return f"完了予定日: {year}年{month:02d}月{day:02d}日({weekday})"


def _fill_submit_form(module, fields: _Fields, *, project_id: str, amount_minor: int, expire_period_days: int) -> None:
    """The registry entries that let _submit_application's form-fill try-block succeed, so every
    test below reaches the post-click navigation logic under test."""
    fields.set(module._FORM_SELECTOR, _Field(fields, attrs={"method": "post", "action": "/proposals"}))
    fields.set(
        'input#proposal_job_offer_id[type="hidden"]',
        _Field(fields, attrs={"type": "hidden"}, value=project_id),
    )
    for selector, expected in (
        ("#without_condition_false", "false"),
        ("#proposal_conditions_attributes_0_payment_type_fixed_price", "fixed_price"),
        ("#how_to_present_fixed_price_contract_amount", "contract_amount"),
    ):
        fields.set(
            f'input{selector}[type="radio"][value="{expected}"]',
            _Field(fields, attrs={"type": "radio", "value": expected}),
        )
    fields.set('input#amount_dummy_[type="text"]', _Field(fields, attrs={"type": "text"}))
    fields.set(
        'input#proposal_conditions_attributes_0_milestones_attributes_0_amount_without_sales_tax[type="hidden"]',
        _Field(fields, attrs={"type": "hidden"}, value=str(amount_minor)),
    )
    for suffix in ("1i", "2i", "3i"):
        fields.set(f'select[id$="deadline_{suffix}"]', _Field(fields))
    fields.set("textarea#proposal_conditions_attributes_0_message_attributes_body", _Field(fields))
    fields.set(
        'select#expire_period[name="expire_period"]',
        _Field(fields, value=str(expire_period_days)),
    )
    fields.set('input[name="commit"][type="submit"]', _Field(fields, enabled=True))


def _add_proposal_list(module, fields: _Fields, *, proposal_ids: list[str]) -> None:
    items = [_Field(fields, attrs={"href": f"/proposals/{pid}"}) for pid in proposal_ids]
    fields.set(module._TABLE_SELECTOR, _Field(fields, count=1))
    fields.set('a[href^="/proposals/"]', _Field(fields, count=len(items), items=items))


def _add_matching_proposal_detail(module, fields: _Fields, *, project_id: str, amount_minor: int, delivery_due_on: str) -> None:
    fields.set(module._PROJECT_SELECTOR.format(project_id=project_id), _Field(fields, count=1))
    fields.set(module._AMOUNT_SELECTOR, _Field(fields, value=f"固定報酬: {amount_minor:,}円"))
    fields.set(module._DUE_SELECTOR, _Field(fields, value=_due_line(delivery_due_on)))
    fields.set(module._PROGRESS_SELECTOR, _Field(fields, value="応募・スカウト"))


def _build_page(module, *, wait_for_url=None, goto_errors=None, extra=None) -> _Page:
    fields = _Fields()
    _fill_submit_form(
        module, fields, project_id=PROJECT_ID, amount_minor=AMOUNT_MINOR, expire_period_days=EXPIRE_PERIOD_DAYS
    )
    if extra is not None:
        extra(fields)
    return _Page(
        fields,
        url=f"https://crowdworks.jp/proposals/new?job_offer_id={PROJECT_ID}",
        wait_for_url=wait_for_url,
        goto_errors=goto_errors,
    )


def _submit(module, page: _Page):
    return module._submit_application(
        page,
        {"external_id": PROJECT_ID},
        PROPOSAL_TEXT,
        AMOUNT_MINOR,
        DELIVERY_DUE_ON,
        EXPIRE_PERIOD_DAYS,
    )


# 1. Navigation lands normally -> proposal id returned, list never consulted.
def test_normal_landing_returns_proposal_id_without_consulting_list():
    module = load()

    def wait_for_url(pattern, timeout=None):
        page.url = "https://crowdworks.jp/proposals/304582247#scroll_to_message"

    page = _build_page(module, wait_for_url=wait_for_url)

    result = _submit(module, page)

    assert result == {"proposal_id": "304582247"}
    assert module._PROPOSAL_LIST_URL not in page.goto_log


# 2. Navigation times out, list shows a proposal for this project -> same proposal id returned,
#    result records it was confirmed from the list.
def test_timeout_confirmed_from_list_returns_same_proposal_id_marked_list_confirmed():
    module = load()

    def wait_for_url(pattern, timeout=None):
        raise TimeoutError("navigation timed out")

    def extra(fields):
        _add_proposal_list(module, fields, proposal_ids=["304582247"])
        # A second page is normal once the account has more than one page of applications. The
        # current project is still visible on the first page and must be usable as official
        # readback instead of making the whole list unreadable.
        fields.set('a[href*="/e/proposals?page="]', _Field(fields, count=1))
        fields.set('a[rel="next"]', _Field(fields, count=1))
        _add_matching_proposal_detail(
            module, fields, project_id=PROJECT_ID, amount_minor=AMOUNT_MINOR, delivery_due_on=DELIVERY_DUE_ON
        )

    page = _build_page(module, wait_for_url=wait_for_url, extra=extra)

    result = _submit(module, page)

    assert result == {"proposal_id": "304582247", "confirmed_via": "list"}
    assert module._PROPOSAL_LIST_URL in page.goto_log


# 3. Navigation times out, list has no proposal for this project -> submission_uncertain raised.
def test_timeout_list_has_no_matching_proposal_raises_submission_uncertain():
    module = load()

    def wait_for_url(pattern, timeout=None):
        raise TimeoutError("navigation timed out")

    def extra(fields):
        # The list shows a proposal, just not one for this project: no project-selector match is
        # registered, so _read_proposal_detail refuses it.
        _add_proposal_list(module, fields, proposal_ids=["999999999"])

    page = _build_page(module, wait_for_url=wait_for_url, extra=extra)

    with pytest.raises(RuntimeError, match="submission_uncertain"):
        _submit(module, page)

    assert module._PROPOSAL_LIST_URL in page.goto_log


# 4. Navigation times out, list cannot be read (throws) -> submission_uncertain raised, not
#    reported as absence.
def test_timeout_unreadable_list_raises_submission_uncertain_not_absence():
    module = load()

    def wait_for_url(pattern, timeout=None):
        raise TimeoutError("navigation timed out")

    page = _build_page(
        module,
        wait_for_url=wait_for_url,
        goto_errors={module._PROPOSAL_LIST_URL: RuntimeError("navigation_failed")},
    )

    with pytest.raises(RuntimeError, match="submission_uncertain"):
        _submit(module, page)

    # The list read was actually attempted (and failed) rather than skipped and defaulted to
    # "not found" -- an unreadable list must never be treated as proof of absence.
    assert module._PROPOSAL_LIST_URL in page.goto_log


# 5. The landed URL parses to something that is not /proposals/<id> -> the list is consulted, same
#    as a timeout.
def test_landed_url_does_not_parse_consults_list_same_as_timeout():
    module = load()

    def wait_for_url(pattern, timeout=None):
        # wait_for_url itself is satisfied, but the browser lands with a query string, which the
        # stricter post-navigation check in _submit_application refuses.
        page.url = "https://crowdworks.jp/proposals/304582247?ref=mypage"

    def extra(fields):
        _add_proposal_list(module, fields, proposal_ids=["304582247"])
        _add_matching_proposal_detail(
            module, fields, project_id=PROJECT_ID, amount_minor=AMOUNT_MINOR, delivery_due_on=DELIVERY_DUE_ON
        )

    page = _build_page(module, wait_for_url=wait_for_url, extra=extra)

    result = _submit(module, page)

    assert result == {"proposal_id": "304582247", "confirmed_via": "list"}
    assert module._PROPOSAL_LIST_URL in page.goto_log


# 6. A form that never opened still raises SubmissionNotStarted, not submission_uncertain.
def test_form_never_opened_raises_submission_not_started_not_uncertain():
    module = load()
    # No form-fill fields registered at all: the very first _one(page, _FORM_SELECTOR) call fails.
    page = _Page(_Fields(), url=f"https://crowdworks.jp/proposals/new?job_offer_id={PROJECT_ID}")

    with pytest.raises(module.shared.SubmissionNotStarted) as caught:
        _submit(module, page)

    assert caught.value.error == "proposal_form_changed"
    assert module._PROPOSAL_LIST_URL not in page.goto_log


# 7. The list walk has one implementation and does not reject normal pagination.
def test_list_walk_selectors_are_not_duplicated():
    source = PATH.read_text(encoding="utf-8")
    assert source.count("_one(page, _TABLE_SELECTOR).locator(") == 1
    assert source.count('a[href^="/proposals/"]') == 1
    assert 'a[href*="/e/proposals?page="]' not in source
    assert 'a[rel="next"]' not in source
