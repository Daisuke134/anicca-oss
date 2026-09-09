#!/usr/bin/env python3
"""CrowdWorks wrapper for the shared application transaction coordinator."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
from datetime import date
from pathlib import Path
import re
import sys
from typing import Callable, Mapping
from urllib.parse import urlsplit


_SHARED_PATH = (
    Path(__file__).resolve().parents[3]
    / "_shared"
    / "marketplace-core"
    / "scripts"
    / "application_transaction.py"
)
_SHARED_MODULE_NAME = "anicca_crowdworks_shared_application_transaction"


def _load_shared():
    if _SHARED_MODULE_NAME in sys.modules:
        return sys.modules[_SHARED_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_SHARED_MODULE_NAME, _SHARED_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("marketplace_application_transaction_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SHARED_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


shared = _load_shared()
TickResult = shared.TickResult
account_lock = shared.account_lock
load_marketplace_contracts = shared.load_marketplace_contracts


_ASCII_DIGITS = re.compile(r"^[0-9]+$")
_AMOUNT = re.compile(r"^固定報酬: (?P<amount>[1-9][0-9]{0,2}(?:,[0-9]{3})*)円$")
_DUE = re.compile(r"^完了予定日: (?P<year>[0-9]{4})年(?P<month>0[1-9]|1[0-2])月(?P<day>0[1-9]|[12][0-9]|3[01])日\((?P<weekday>[月火水木金土日])\)$")
_HOURLY_RATE = re.compile(r"時間単価:\s*(?P<amount>[1-9][0-9]{0,2}(?:,[0-9]{3})*)円")
_WEEKLY_LIMIT = re.compile(r"(?:週(?:上限)?\s*|稼働時間/週:\s*)(?P<hours>[1-9][0-9]*)\s*時間")
_PROJECT_SELECTOR = 'a[href="/public/jobs/{project_id}"]'
_AMOUNT_SELECTOR = ".intro-employer_proposed_project > table.conditions span.quotation_price"
_DUE_SELECTOR = ".intro-employer_proposed_project > table.conditions div.deadline"
_LATEST_CONDITION_SELECTOR = ".intro-employer_proposed_project > table.conditions tr:last-child td.condition"
_PROGRESS_SELECTOR = ".cw-global_row > .progress ul.progress > li.current:nth-of-type(2)"
_BODY_SELECTOR = "._messageSent_iq3vm_6._message_iq3vm_2 > ._messageBody_iq3vm_28:nth-of-type(2) > ._messageContent_iq3vm_35 > ._mediaBody_iq3vm_10:nth-of-type(2) > div:nth-of-type(2) > p"
_READBACK_FAILED = TickResult(ok=False, error="provider_application_readback_failed")
_PROPOSAL_LIST_URL = "https://crowdworks.jp/e/proposals"
_EXPIRE_SELECTOR = "#expire_period"
_FORM_SELECTOR = 'form#new_proposal[action="/proposals"][method="post"]'
_TABLE_SELECTOR = "body.employee-proposals.employee-proposals-index .applications.section > table.proposals"
def _one(page: object, selector: str):
    locator = page.locator(selector)  # type: ignore[attr-defined]
    if type(count := locator.count()) is not int or count != 1:
        raise ValueError("selector_unobserved")
    return locator
def _value(locator: object) -> str:
    if not isinstance(value := locator.input_value(), str):  # type: ignore[attr-defined]
        raise ValueError("field_unobserved")
    return value
def _exact_url(raw_url: object, path: str, query: str = "") -> bool:
    try:
        parsed = urlsplit(raw_url) if isinstance(raw_url, str) else None
        return parsed is not None and parsed.scheme == "https" and parsed.hostname == "crowdworks.jp" and parsed.username is None and parsed.password is None and parsed.port in (None, 443) and parsed.path == path and parsed.query == query and not parsed.fragment and not (query == "" and "?" in raw_url) and not ("#" in raw_url and parsed.fragment == "")
    except (TypeError, ValueError):
        return False
def _exclusive(amount: int) -> int:
    """Proposals are submitted tax-exclusive but 固定報酬 reads back tax-inclusive (300,000 → 330,000),
    so verification compared 330,000 against the recorded 300,000 and called every real application
    submission_uncertain. Reverse the tax only when it reverses exactly."""
    base = round(amount / 1.1)
    return base if round(base * 1.1) == amount else amount
def _read_proposal_detail(page: object, proposal_id: str, project_id: str, *, include_body: bool = False) -> Mapping[str, object]:
    if _ASCII_DIGITS.fullmatch(proposal_id) is None or _ASCII_DIGITS.fullmatch(project_id) is None:
        raise ValueError("identity_unobserved")
    page.goto(f"https://crowdworks.jp/proposals/{proposal_id}")  # type: ignore[attr-defined]
    if not _exact_url(getattr(page, "url", None), f"/proposals/{proposal_id}"): raise ValueError("route_unobserved")
    _one(page, _PROJECT_SELECTOR.format(project_id=project_id))
    condition = re.sub(r"\s+", " ", _one_text(page, _LATEST_CONDITION_SELECTOR)).strip()
    hourly, weekly = _HOURLY_RATE.search(condition), _WEEKLY_LIMIT.search(condition)
    if hourly is not None and weekly is not None:
        observed: dict[str, object] = {"proposal_id": proposal_id, "project_id": project_id, "pricing_mode": "hourly", "hourly_rate_minor": _exclusive(int(hourly.group("amount").replace(",", ""))), "weekly_limit_hours": int(weekly.group("hours"))}
    else:
        amount = _AMOUNT.fullmatch(_one_text(page, _AMOUNT_SELECTOR))
        due = _DUE.fullmatch(_one_text(page, _DUE_SELECTOR))
        if amount is None or due is None: raise ValueError("terms_unobserved")
        parsed_due = date(int(due.group("year")), int(due.group("month")), int(due.group("day")))
        if due.group("weekday") != "月火水木金土日"[parsed_due.weekday()]: raise ValueError("terms_unobserved")
        observed = {"proposal_id": proposal_id, "project_id": project_id, "amount_minor": _exclusive(int(amount.group("amount").replace(",", ""))), "delivery_due_on": parsed_due.isoformat()}
    if re.sub(r"\s+", " ", _one_text(page, _PROGRESS_SELECTOR)).strip() != "応募・スカウト": raise ValueError("state_unobserved")
    if include_body:
        # Collapsing whitespace here turned the submitted line breaks into spaces, so the content
        # fingerprint could never match what was sent and every application verified as uncertain
        # forever. CrowdWorks returns the body verbatim; keep it that way.
        body = _one_text(page, _BODY_SELECTOR).replace("\r\n", "\n").strip()
        if not body: raise ValueError("body_unobserved")
        observed["_proposal_text"] = body
    return observed
def _one_text(page: object, selector: str) -> str:
    locator = page.locator(selector)  # type: ignore[attr-defined]
    count = locator.count()
    value = locator.inner_text() if type(count) is int and count == 1 else None
    if not isinstance(value, str):
        raise ValueError("selector_unobserved")
    return value


def _confirmed_from_list(page: object, project_id: str) -> str | None:
    """The proposal list walk shared by every reader of the worker's own CrowdWorks list: navigate
    to it, collect the proposal links inside the applications table, and return the id of the one
    that names this project.

    Returns None only when the list was actually read and does not show this project. Raises when
    the list itself -- the navigation, the table, a paginated result -- could not be read, so a
    caller never mistakes an unreadable list for evidence that nothing was submitted.
    """
    page.goto(_PROPOSAL_LIST_URL)  # type: ignore[attr-defined]
    if not _exact_url(getattr(page, "url", None), "/e/proposals"): raise RuntimeError("proposal_list_unreadable")
    # Pagination does not make the visible page unreadable. CrowdWorks keeps the newest
    # applications on page one, so rejecting the entire list merely because a page-two link exists
    # strands every newly submitted application as uncertain. Read the authoritative visible rows;
    # older pending entries can remain fenced until a later bounded page-walk is needed.
    links = _one(page, _TABLE_SELECTOR).locator('a[href^="/proposals/"]')
    count = links.count()
    if type(count) is not int: raise RuntimeError("proposal_list_unreadable")
    identities = set()
    for index in range(count):
        parsed = urlsplit(links.nth(index).get_attribute("href") or "")
        match = re.fullmatch(r"/proposals/([0-9]+)", parsed.path)
        if match is not None and not (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment): identities.add(match.group(1))
    matches = []
    for identity in sorted(identities):
        try:
            _read_proposal_detail(page, identity, project_id)
        except Exception:
            continue
        matches.append(identity)
    return matches[0] if len(matches) == 1 else None
def find_proposal_id(page: object, project_id: str) -> str | None:
    """The proposal id CrowdWorks holds for this project, read from the worker's own list.

    A submission whose landing page was not recognised leaves the transaction pending with no
    proposal id, so without this the application exists on CrowdWorks and never reaches the ledger.
    """
    if not isinstance(project_id, str) or _ASCII_DIGITS.fullmatch(project_id) is None: return None
    try:
        return _confirmed_from_list(page, project_id)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception:
        return None
def reconcile_existing_application(*, page: object, proposal_id: str, opportunity: Mapping[str, object], state_path: Path, ledger_writer: Callable[[Mapping[str, object]], object], now: Callable[[], object], account_ready: Callable[[], bool]) -> TickResult:
    """Read one existing provider application and import it without submitting."""
    try:
        project_id = opportunity.get("external_id")
    except Exception: return _READBACK_FAILED
    if not isinstance(proposal_id, str) or _ASCII_DIGITS.fullmatch(proposal_id) is None or not isinstance(project_id, str) or _ASCII_DIGITS.fullmatch(project_id) is None:
        return _READBACK_FAILED
    try:
        detail = _read_proposal_detail(page, proposal_id, project_id, include_body=True)
    except (KeyboardInterrupt, SystemExit, MemoryError):
        raise
    except Exception:
        return _READBACK_FAILED
    proposal_text = detail.pop("_proposal_text")
    observed = dict(detail)
    hourly = detail.get("pricing_mode") == "hourly"
    result = shared.run_transaction(
        platform="crowdworks",
        opportunity=opportunity,
        proposal_text=proposal_text,
        proposed_amount_minor=observed["hourly_rate_minor" if hourly else "amount_minor"],
        delivery_due_on=None if hourly else observed["delivery_due_on"],
        pricing_mode="hourly" if hourly else "fixed",
        weekly_limit_hours=observed.get("weekly_limit_hours") if hourly else None,
        state_path=state_path,
        account_ready=account_ready,
        submitter=lambda *_args: {"proposal_id": proposal_id},
        readback=lambda _proposal, _project: observed,
        ledger_writer=ledger_writer,
        now=now,
    )
    return replace(result, submitted=False)
def _confirm_after_uncertain_navigation(page: object, project_id: object) -> Mapping[str, object]:
    """The post-submit navigation could not be trusted on its own -- it timed out, or it landed
    somewhere that did not parse as /proposals/<id>. A slow or unrecognised navigation is not
    evidence that nothing was submitted, so ask CrowdWorks' own proposal list -- the same list-walk
    find_proposal_id and _readback_application use -- before concluding submission_uncertain.

    A submission confirmed this way carries confirmed_via="list" so a reader can tell it apart from
    one confirmed by the redirect, but the proposal id is returned exactly as the fast-navigation
    path would, so the receipt, the transaction and application_verified are unaffected.
    """
    if isinstance(project_id, str):
        try:
            identity = _confirmed_from_list(page, project_id)
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception:
            identity = None
        if identity is not None:
            return {"proposal_id": identity, "confirmed_via": "list"}
    raise RuntimeError("submission_uncertain") from None
def _submit_application(page: object, opportunity: Mapping[str, object], proposal_text: str, amount_minor: int, delivery_due_on: str | None, expire_period_days: int | None, *, pricing_mode: str = "fixed", weekly_limit_hours: int | None = None) -> Mapping[str, object]:
    project_id = opportunity.get("external_id")
    try:
        form_url = f"https://crowdworks.jp/proposals/new?job_offer_id={project_id}"
        # The owner hands over a page parked on the job detail page, not a blank one, so navigating
        # only from about:blank left every submission failing the route check below.
        if not _exact_url(getattr(page, "url", None), "/proposals/new", f"job_offer_id={project_id}"): page.goto(form_url)  # type: ignore[attr-defined]
        if not isinstance(project_id, str) or not _exact_url(getattr(page, "url", None), "/proposals/new", f"job_offer_id={project_id}"): raise ValueError("route")
        form = _one(page, _FORM_SELECTOR)
        if str(form.get_attribute("method") or "").lower() != "post" or form.get_attribute("action") != "/proposals": raise ValueError("form")
        job = _one(form, 'input#proposal_job_offer_id[type="hidden"]')
        if job.get_attribute("type") != "hidden" or _value(job) != project_id: raise ValueError("job")
        mode_controls = (("#proposal_conditions_attributes_0_payment_type_hourly", "hourly"), ("#how_to_present_hourly_contract_amount", "contract_amount")) if pricing_mode == "hourly" else (("#proposal_conditions_attributes_0_payment_type_fixed_price", "fixed_price"), ("#how_to_present_fixed_price_contract_amount", "contract_amount"))
        for selector, expected in (("#without_condition_false", "false"), *mode_controls):
            item = _one(form, f'input{selector}[type="radio"][value="{expected}"]')
            if item.get_attribute("type") != "radio" or item.get_attribute("value") != expected or not callable(getattr(item, "check", None)): raise ValueError("payment")
            item.check()
        if pricing_mode == "hourly":
            amount = _one(form, 'input#hourly_wage_dummy_[type="text"]')
            amount.fill(str(amount_minor)); amount.blur()
            if _value(_one(form, 'input#proposal_conditions_attributes_0_hourly_wage_without_sales_tax[type="hidden"]')) != str(amount_minor): raise ValueError("amount")
            hours = _one(form, 'input#proposal_conditions_attributes_0_hours_limit[type="text"]')
            hours.fill(str(weekly_limit_hours)); hours.blur()
            if _value(hours) != str(weekly_limit_hours): raise ValueError("hours")
        else:
            amount = _one(form, 'input#amount_dummy_[type="text"]')
            amount.fill(str(amount_minor)); amount.blur()
            if _value(_one(form, 'input#proposal_conditions_attributes_0_milestones_attributes_0_amount_without_sales_tax[type="hidden"]')) != str(amount_minor): raise ValueError("amount")
            year, month, day = delivery_due_on.split("-")
            for suffix, expected in (("1i", year), ("2i", month), ("3i", day)):
                item = _one(form, f'select[id$="deadline_{suffix}"]')
                selected_value = expected if suffix == "1i" else str(int(expected))
                item.select_option(selected_value)
                if _value(item) != selected_value: raise ValueError("due")
        body = _one(form, "textarea#proposal_conditions_attributes_0_message_attributes_body")
        body.fill(proposal_text)
        if _value(body) != proposal_text: raise ValueError("body")
        expiry = _one(form, 'select#expire_period[name="expire_period"]')
        if expire_period_days is None and _value(expiry) not in ("", "0"):
            raise ValueError("expiry")
        elif expire_period_days is not None:
            expiry.select_option(str(expire_period_days))
            if _value(expiry) != str(expire_period_days): raise ValueError("expiry")
        submit = _one(form, 'input[name="commit"][type="submit"]')
        if not submit.is_enabled(): raise ValueError("submit")
    except Exception:
        raise shared.SubmissionNotStarted("proposal_form_changed") from None
    try:
        submit.click(no_wait_after=True)
        wait_for_url = getattr(page, "wait_for_url", None)
        if not callable(wait_for_url): raise RuntimeError("navigation")
        # CrowdWorks lands the accepted proposal on /proposals/<id>#scroll_to_message. Refusing the
        # fragment reported submission_uncertain for proposal 304582247, which had in fact posted.
        wait_for_url(re.compile(r"^https://crowdworks\.jp/proposals/[0-9]+(?:#[^?]*)?$"), timeout=10_000)
    except Exception:
        return _confirm_after_uncertain_navigation(page, project_id)
    try:
        parsed = urlsplit(getattr(page, "url", None))
        landed = parsed.scheme == "https" and parsed.hostname == "crowdworks.jp" and parsed.port in (None, 443) and not parsed.query and parsed.username is None and parsed.password is None
        match = re.fullmatch(r"/proposals/([0-9]+)", parsed.path) if landed else None
    except (TypeError, ValueError):
        match = None
    if match is None:
        return _confirm_after_uncertain_navigation(page, project_id)
    return {"proposal_id": match.group(1)}
def _readback_application(page: object, proposal_id: str | None, project_id: str) -> Mapping[str, object]:
    try:
        if proposal_id is not None:
            # The proposal page is read immediately after submitting it and is not always settled
            # yet, which reported submission_uncertain for applications CrowdWorks had accepted.
            for attempt in range(3):
                try:
                    return _read_proposal_detail(page, proposal_id, project_id)
                except Exception:
                    if attempt == 2: raise
                    wait = getattr(page, "wait_for_timeout", None)
                    if callable(wait): wait(2_000)
        identity = _confirmed_from_list(page, project_id)
        return _read_proposal_detail(page, identity, project_id) if identity is not None else {}
    except (KeyboardInterrupt, SystemExit, MemoryError): raise
    except Exception:
        return {}
def execute_application(*, page: object, opportunity: Mapping[str, object], proposal_text: str, proposed_amount_minor: int, delivery_due_on: str, expire_period_days: int | None, state_path: Path, ledger_writer: Callable[[Mapping[str, object]], object], now: Callable[[], object], account_ready: Callable[[], bool]) -> TickResult:
    """Submit one fixed-price intent once and verify it from CrowdWorks."""
    project_id = opportunity.get("external_id") if isinstance(opportunity, Mapping) else None
    try:
        valid = isinstance(project_id, str) and _ASCII_DIGITS.fullmatch(project_id) is not None and isinstance(proposal_text, str) and bool(proposal_text) and isinstance(proposed_amount_minor, int) and not isinstance(proposed_amount_minor, bool) and proposed_amount_minor > 0 and isinstance(delivery_due_on, str) and date.fromisoformat(delivery_due_on).isoformat() == delivery_due_on and (expire_period_days is None or (isinstance(expire_period_days, int) and not isinstance(expire_period_days, bool) and expire_period_days > 0))
    except (TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        return TickResult(ok=False, error="proposal_form_changed", project_id=project_id if isinstance(project_id, str) else None)
    return run_tick(
        opportunity=opportunity,
        proposal_text=proposal_text,
        proposed_amount_minor=proposed_amount_minor,
        delivery_due_on=delivery_due_on,
        state_path=state_path,
        account_ready=account_ready,
        submitter=lambda source, text, amount, due: _submit_application(page, source, text, amount, due, expire_period_days),
        readback=lambda proposal, project: _readback_application(page, proposal, project),
        ledger_writer=ledger_writer,
        now=now,
    )
def execute_hourly_application(*, page: object, opportunity: Mapping[str, object], proposal_text: str, hourly_rate_minor: int, weekly_limit_hours: int, expire_period_days: int | None, state_path: Path, ledger_writer: Callable[[Mapping[str, object]], object], now: Callable[[], object], account_ready: Callable[[], bool]) -> TickResult:
    project_id = opportunity.get("external_id") if isinstance(opportunity, Mapping) else None
    valid = isinstance(project_id, str) and _ASCII_DIGITS.fullmatch(project_id) is not None and isinstance(hourly_rate_minor, int) and not isinstance(hourly_rate_minor, bool) and hourly_rate_minor > 0 and isinstance(weekly_limit_hours, int) and not isinstance(weekly_limit_hours, bool) and weekly_limit_hours > 0
    if not valid: return TickResult(ok=False, error="proposal_form_changed", project_id=project_id if isinstance(project_id, str) else None)
    return run_tick(opportunity=opportunity, proposal_text=proposal_text, proposed_amount_minor=hourly_rate_minor, delivery_due_on=None, pricing_mode="hourly", weekly_limit_hours=weekly_limit_hours, state_path=state_path, account_ready=account_ready, submitter=lambda source, text, rate, _due: _submit_application(page, source, text, rate, None, expire_period_days, pricing_mode="hourly", weekly_limit_hours=weekly_limit_hours), readback=lambda proposal, project: _readback_application(page, proposal, project), ledger_writer=ledger_writer, now=now)
def run_tick(**kwargs):
    return shared.run_transaction(platform="crowdworks", **kwargs)


__all__ = [
    "TickResult",
    "find_proposal_id",
    "account_lock",
    "load_marketplace_contracts",
    "reconcile_existing_application",
    "execute_application",
    "execute_hourly_application",
    "run_tick",
]
