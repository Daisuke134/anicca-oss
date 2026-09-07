#!/usr/bin/env python3
"""Lancers coordinator plus the measured one-shot browser boundary."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Optional, Sequence
import urllib.request
from urllib.parse import quote, urlsplit


_SHARED_PATH = Path(__file__).resolve().parents[3] / "_shared" / "marketplace-core" / "scripts" / "application_transaction.py"
_SHARED_MODULE_NAME = "anicca_lancers_shared_application_transaction"
CDP_URL = "http://127.0.0.1:9227"
BROWSER_ATTACH_TIMEOUT_MS = 10_000; CDP_REQUEST_TIMEOUT_SECONDS = 2; MAX_CDP_TARGETS = 32; MAX_CDP_RESPONSE_BYTES = 256 * 1024
PLATFORM = "lancers"
DASHBOARD_URL = "https://www.lancers.jp/mypage"
DEFAULT_STATE_PATH = Path.home() / ".local" / "state" / "anicca" / "lancers" / "application.json"
TERMINAL_STATE_RECORD_TYPE = "application_terminal_state"
TERMINAL_STATE_STATUS = "provider_terminal_blocked"


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
SubmissionNotStarted = shared.SubmissionNotStarted
account_lock = shared.account_lock
load_marketplace_contracts = shared.load_marketplace_contracts
read_pending_descriptor = shared.read_pending_descriptor
read_pending_descriptors = shared.read_pending_descriptors


def run_tick(**kwargs):
    return shared.run_transaction(platform=PLATFORM, **kwargs)


def _count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return 0


FORM_EVIDENCE = Path("~/.local/state/anicca/lancers/proposal-form-changes.jsonl").expanduser()


def _record_form_change(locator: Any, why: str, found: Any = None) -> None:
    """Name the selector that stopped matching. Never raises.

    Every strict matcher in this file raises the same bare `proposal_form_changed`, so a single
    changed attribute anywhere in the proposal form produces an error that cannot be acted on.
    Measured 2026-09-07: 81 of the recent Lancers wake summaries skipped with that one code, against
    7 genuine declines -- the lane was not short of work, it could not fill the form, and nothing
    recorded which field it was.
    """
    try:
        row = {
            "selector": str(locator)[:300],
            "why": why,
            "found": found,
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        FORM_EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        with open(FORM_EVIDENCE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        return


def _form_changed(where: str, **detail: Any) -> RuntimeError:
    """Record where the proposal form stopped matching, then hand back the error to raise.

    Measured 2026-09-07: this file raises `proposal_form_changed` from 41 places and only three
    of them -- the two strict matchers above -- recorded anything, so the evidence file this was
    built for stayed empty while 9 of 45 eligible projects in 120 wakes were lost to that one
    code. An error name shared by 41 sites is a name that identifies nothing.

    Returning the exception rather than raising it keeps every call site a `raise`, so control
    flow reads the same as before and no site can record without raising.
    """
    _record_form_change(where, "form_step_failed", detail or None)
    return RuntimeError("proposal_form_changed")


def _one(locator: Any) -> Any:
    found = _count(locator)
    if found != 1:
        _record_form_change(locator, "count_not_one", found)
        raise RuntimeError("proposal_form_changed")
    return locator


def _visible_one(locator: Any) -> Any:
    value = _one(locator)
    # The visibility probe is separated from the verdict on purpose. The old shape re-raised any
    # RuntimeError before recording, so a locator that threw on is_visible() -- a detached node,
    # the usual sign that the form was re-rendered -- was the one case that stayed anonymous.
    try:
        visible = value.is_visible()
    except Exception:
        _record_form_change(locator, "visibility_check_failed", None)
        raise RuntimeError("proposal_form_changed") from None
    if not visible:
        _record_form_change(locator, "not_visible", 1)
        raise RuntimeError("proposal_form_changed")
    return value


def _route(url: Any, path: str, query: Optional[str] = None) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https" and parsed.hostname == "www.lancers.jp"
        and parsed.port is None and parsed.username is None and parsed.password is None
        and parsed.path == path
        and not parsed.fragment
        and (parsed.query == query if query is not None else not parsed.query and not parsed.fragment)
    )


def _url(path: str) -> str:
    return "https://www.lancers.jp" + path


def _terminal_state_path(state_path: Path) -> Path:
    path = Path(state_path)
    suffix = path.suffix or ".json"
    return path.with_name(path.stem + ".terminal" + suffix)


def _read_terminal_state(state_path: Path) -> dict[str, dict[str, str]]:
    path = _terminal_state_path(state_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("state_invalid") from exc
    blocked = raw.get("terminal_blocked") if isinstance(raw, Mapping) else None
    if not isinstance(raw, Mapping) or raw.get("record_type") != TERMINAL_STATE_RECORD_TYPE or not isinstance(blocked, Mapping):
        raise RuntimeError("state_invalid")
    result: dict[str, dict[str, str]] = {}
    for marker, entry in blocked.items():
        if (
            not isinstance(marker, str)
            or re.fullmatch(r"[0-9a-f]{64}", marker) is None
            or not isinstance(entry, Mapping)
            or entry.get("status") != TERMINAL_STATE_STATUS
            or not isinstance(entry.get("observed_at"), str)
            or not entry["observed_at"].strip()
        ):
            raise RuntimeError("state_invalid")
        result[marker] = dict(entry)
    return result


def _write_terminal_state(state_path: Path, terminal: Mapping[str, Mapping[str, str]]) -> None:
    path = _terminal_state_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_type": TERMINAL_STATE_RECORD_TYPE,
        "terminal_blocked": {marker: dict(entry) for marker, entry in sorted(terminal.items())},
    }
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=str(path.parent), prefix="." + path.name + ".", delete=False,
            encoding="utf-8",
        ) as handle:
            temporary_name = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _state_has_terminal_block(state_path: Path, project_id: str) -> bool:
    return _application_marker(project_id) in _read_terminal_state(Path(state_path))


def _record_terminal_block(
    state_path: Path, project_id: str, now: Callable[[], object]
) -> None:
    path = Path(state_path)
    marker = _application_marker(project_id)
    with account_lock(path):
        terminal = _read_terminal_state(path)
        claims, pending = shared._read_state(path)
        if marker in terminal:
            if marker not in claims:
                claims.add(marker)
                shared._write_state(path, claims, pending)
            return
        if marker in claims:
            raise RuntimeError("state_invalid")
        observed_at = now()
        if not isinstance(observed_at, str) or not observed_at.strip():
            raise RuntimeError("state_invalid")
        terminal[marker] = {
            "status": TERMINAL_STATE_STATUS,
            "idempotency_key": f"{PLATFORM}:application_terminal:{marker}:v1",
            "observed_at": observed_at,
        }
        _write_terminal_state(path, terminal)
        claims.add(marker)
        shared._write_state(path, claims, pending)


def _stop_playwright_runtime(runtime: Any) -> None:
    try:
        getattr(runtime, "stop", lambda: None)()
    except Exception:
        pass


_LANCERS_AUTH_ROUTES = {"/user/login", "/user/reminder"}; _GOOGLE_AUTH_ROUTES = {"/v3/signin/accountchooser", "/info/sessionexpired"}


def _cdp_request(url: str, limit: Optional[int] = None) -> Any:
    response = None
    try:
        response = urllib.request.urlopen(url, timeout=CDP_REQUEST_TIMEOUT_SECONDS)
        if getattr(response, "status", 200) != 200:
            return None
        if limit is None:
            return True
        raw = response.read(limit + 1)
        return raw if isinstance(raw, (bytes, bytearray)) and len(raw) <= limit else None
    except Exception:
        return None
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass


def _cdp_inventory(cdp_url: str) -> Optional[list[tuple[str, str]]]:
    if cdp_url != CDP_URL: return None
    raw = _cdp_request(f"{cdp_url}/json/list", MAX_CDP_RESPONSE_BYTES)
    try:
        value = json.loads(bytes(raw).decode("utf-8")) if raw is not None else None
    except Exception:
        return None
    if not isinstance(value, list) or len(value) > MAX_CDP_TARGETS:
        return None
    seen: set[str] = set(); targets: list[tuple[str, str]] = []
    for target in value:
        if not isinstance(target, Mapping):
            return None
        target_id, target_url = target.get("id"), target.get("url")
        if (
            not isinstance(target_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", target_id)
            or target_id in seen or not isinstance(target_url, str) or not 1 <= len(target_url) <= 4096
            or any(ord(char) < 0x20 for char in target_url) or ("type" in target and not isinstance(target["type"], str))
        ):
            return None
        try:
            urlsplit(target_url)
        except Exception:
            return None
        seen.add(target_id)
        if target.get("type", "page") != "page":
            continue
        targets.append((target_id, target_url))
    return targets


def _safe_lancers_url(value: str) -> Optional[Any]:
    try: parsed = urlsplit(value); return parsed if parsed.scheme == "https" and parsed.hostname == "www.lancers.jp" and parsed.port is None and parsed.username is None and parsed.password is None and not parsed.fragment else None
    except Exception: return None


def _stale_auth_target(value: str) -> bool:
    if value == "chrome://ungoogled-first-run/":
        return True
    parsed = _safe_lancers_url(value)
    if parsed:
        return parsed.path in _LANCERS_AUTH_ROUTES
    try: parsed = urlsplit(value); return parsed.scheme == "https" and parsed.hostname == "accounts.google.com" and parsed.port is None and parsed.username is None and parsed.password is None and not parsed.fragment and parsed.path in _GOOGLE_AUTH_ROUTES
    except Exception: return False


def _cleanup_stale_targets(cdp_url: str) -> bool:
    targets = _cdp_inventory(cdp_url)
    stale = [target_id for target_id, url in targets if _safe_lancers_url(url) or _stale_auth_target(url)]
    return bool(stale) and all(_cdp_request(f"{cdp_url}/json/close/{quote(target_id, safe='')}") for target_id in stale)


def _default_browser_factory(cdp_url: str = CDP_URL) -> Any:
    if cdp_url != CDP_URL:
        raise RuntimeError("browser_endpoint_invalid")
    runtime = None
    try:
        from playwright.sync_api import sync_playwright
        runtime = sync_playwright().start()
        browser = runtime.chromium.connect_over_cdp(cdp_url, timeout=BROWSER_ATTACH_TIMEOUT_MS)
        setattr(browser, "_anicca_playwright_runtime", runtime)
        return browser
    except Exception as exc:
        _stop_playwright_runtime(runtime)
        try: from playwright.sync_api import TimeoutError as PlaywrightTimeoutError; is_timeout = isinstance(exc, PlaywrightTimeoutError)
        except Exception: is_timeout = False
        if runtime is None or not is_timeout: raise RuntimeError("browser_connect_failed") from None
    if runtime is None or not _cleanup_stale_targets(cdp_url):
        raise RuntimeError("browser_connect_failed") from None
    retry_runtime = None
    try:
        retry_runtime = sync_playwright().start(); browser = retry_runtime.chromium.connect_over_cdp(cdp_url, timeout=BROWSER_ATTACH_TIMEOUT_MS)
        setattr(browser, "_anicca_playwright_runtime", retry_runtime)
        return browser
    except Exception:
        _stop_playwright_runtime(retry_runtime)
        raise RuntimeError("browser_connect_failed") from None


def _new_owned_page(browser: Any) -> Any:
    contexts = getattr(browser, "contexts", ())
    if not contexts or not callable(getattr(contexts[0], "new_page", None)):
        raise RuntimeError("browser_page_unavailable")
    return contexts[0].new_page()


def _open_owned_page(browser_factory: Optional[Callable[[str], Any]] = None) -> tuple[Any, Any]:
    for attempt in range(2):
        browser = None
        try:
            browser = (browser_factory or _default_browser_factory)(CDP_URL)
            return browser, _new_owned_page(browser)
        except Exception as error:
            if attempt:
                print(f"application_tick:browser_open_failed:{type(error).__name__}:{error}", file=sys.stderr)
                raise
            time.sleep(1)
    raise RuntimeError("browser_unavailable")


def _close_owned_page(page: Any) -> bool:
    try:
        if page is not None:
            page.close()
        return True
    except Exception:
        return False


def _production_account_ready(page: Any) -> bool:
    try:
        page.goto(DASHBOARD_URL)
        if getattr(page, "url", None) != DASHBOARD_URL:
            return False
        return page.locator("#login_form").count() == 0
    except Exception:
        return False


def _project_id(opportunity: Mapping[str, object]) -> str:
    value = opportunity.get("external_id") if isinstance(opportunity, Mapping) else None
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise RuntimeError("project_id_invalid")
    return value


def _proposal_href(page: Any, project_id: str) -> str:
    links, matches = page.locator("a"), []
    expected_href = f"/work/propose_start/{project_id}?proposeReferer="
    for index in range(_count(links)):
        link = links.nth(index)
        try:
            href = link.get_attribute("href")
            visible = link.is_visible()
            text = " ".join(link.inner_text().split())
        except Exception:
            continue
        if not visible:
            continue
        if href == expected_href and text != "提案する":
            raise _form_changed("_proposal_href:394")
        if text == "提案する" and (not isinstance(href, str) or href != expected_href):
            raise _form_changed("_proposal_href:396")
        if text != "提案する":
            continue
        raw = _url(href)
        if not _route(raw, f"/work/propose_start/{project_id}", "proposeReferer="):
            raise _form_changed("_proposal_href:401")
        matches.append(raw)
    if not matches or len(set(matches)) != 1:
        raise _form_changed("_proposal_href:404")
    return matches[0]


def _provider_terminal_blocked(page: Any) -> bool:
    """Recognize only the measured provider-side no-proposal marker."""
    get_by_text = getattr(page, "get_by_text", None)
    if callable(get_by_text):
        try:
            exact_text = get_by_text("提案できません", exact=True)
            for index in range(_count(exact_text)):
                if exact_text.nth(index).is_visible():
                    return True
        except Exception:
            pass
    try:
        faq_links = page.locator('a[href="/faq/l1011/87"]')
        for index in range(_count(faq_links)):
            if faq_links.nth(index).is_visible():
                return True
    except Exception:
        pass
    return False


def _exact_page_metadata(page: Any, expected_url: str) -> None:
    for selector, attribute in (
        (f'link[rel="canonical"][href="{expected_url}"]', "href"),
        (f'meta[property="og:url"][content="{expected_url}"]', "content"),
    ):
        if _one(page.locator(selector)).get_attribute(attribute) != expected_url:
            raise _form_changed("_exact_page_metadata:435")


def _production_prepare(
    page: Any, project_id: str, proposed_amount_minor: int, delivery_due_on: str,
) -> Mapping[str, object]:
    """Run the read-only, measured proposal-form preflight.

    This is deliberately separate from the shared transaction.  A new project
    must pass every route/DOM check before that transaction persists its claim.
    """
    if re.fullmatch(r"[0-9]+", project_id) is None:
        raise RuntimeError("project_id_invalid")
    if (
        isinstance(proposed_amount_minor, bool)
        or not isinstance(proposed_amount_minor, int)
        or proposed_amount_minor <= 0
        or _iso_date(delivery_due_on) != delivery_due_on
    ):
        raise RuntimeError("financial_terms_required")
    detail_url = _url(f"/work/detail/{project_id}")
    page.goto(detail_url, wait_until="domcontentloaded", timeout=20_000)
    if not _route(getattr(page, "url", None), f"/work/detail/{project_id}"):
        raise _form_changed("_production_prepare:458")
    _exact_page_metadata(page, detail_url)
    if _provider_terminal_blocked(page):
        raise RuntimeError("provider_terminal_blocked")
    proposal_url = _proposal_href(page, project_id)
    page.goto(proposal_url, wait_until="domcontentloaded", timeout=20_000)
    if not _route(getattr(page, "url", None), f"/work/propose_start/{project_id}", "proposeReferer="):
        raise _form_changed("_production_prepare:465")
    _exact_page_metadata(page, _url(f"/work/propose_start/{project_id}"))

    wait_for_function = getattr(page, "wait_for_function", None)
    if not callable(wait_for_function):
        raise _form_changed("_production_prepare:470")
    fee_selector = f'#FeeApp[data-work-id="{project_id}"]'
    fee_selector_js = json.dumps(fee_selector)
    try:
        wait_for_function(
            f"""() => {{ const fee = document.querySelector({fee_selector_js}); return fee !== null && fee.querySelectorAll('input[type=\\"number\\"][step=\\"1000\\"][max=\\"100000000\\"]').length === 1 && fee.querySelectorAll('input[type=\\"text\\"]').length === 1; }}""",
            timeout=5_000,
        )
    except Exception:
        # Naming the line was enough to find the step; it was not enough to find the posting or to
        # say which of the five ANDed conditions failed, so two more wakes were spent guessing at
        # the type of the work. Record what the page actually had.
        observed: dict[str, Any] = {"project_id": str(project_id), "url": str(getattr(page, "url", ""))[:200]}
        try:
            observed.update(page.evaluate(
                f"""() => {{
                    const fee = document.querySelector({fee_selector_js});
                    return {{
                        fee_present: fee !== null,
                        fee_work_ids: [...document.querySelectorAll('[id="FeeApp"]')].map(e => e.getAttribute('data-work-id')),
                        numbers: fee ? fee.querySelectorAll('input[type="number"]').length : null,
                        numbers_strict: fee ? fee.querySelectorAll('input[type=\"number\"][step=\"1000\"][max=\"100000000\"]').length : null,
                        number_attrs: fee ? [...fee.querySelectorAll('input[type="number"]')].map(e => e.getAttribute('step') + '/' + e.getAttribute('max')) : null,
                        texts: fee ? fee.querySelectorAll('input[type="text"]').length : null,
                        visible_fields: [...document.querySelectorAll('input,textarea,select')]
                            .filter(e => e.type !== 'hidden')
                            .map(e => e.tagName + ':' + e.type + ':' + (e.name || e.id)).slice(0, 12),
                    }};
                }}"""))
        except Exception:
            observed["probe"] = "failed"
        raise _form_changed("_production_prepare:479", **observed) from None
    form = _one(page.locator("form#ProposalProposeForm"))
    if (
        str(form.get_attribute("method") or "").upper() != "POST"
        or form.get_attribute("action") != f"/work/propose_start/{project_id}"
    ):
        raise _form_changed("_production_prepare:485")
    body = _visible_one(form.locator('textarea#ProposalDescription[name="data[Proposal][description]"]'))
    if body.get_attribute("required") is None:
        raise _form_changed("_production_prepare:488")
    fee = _one(page.locator(fee_selector))
    if fee.get_attribute("data-work-id") != project_id:
        raise _form_changed("_production_prepare:491")
    _visible_one(page.locator('#FeeApp input[type="number"][step="1000"][max="100000000"]'))
    _visible_one(page.locator('#FeeApp input[type="text"]'))
    _visible_one(form.locator("#form_end"))
    prepared = {"project_id": project_id, "proposal_url": proposal_url}
    try:
        setattr(page, "_anicca_lancers_prepared", prepared)
    except Exception:
        pass
    return prepared


def _field_value(locator: Any) -> str:
    try:
        value = locator.input_value()
    except Exception:
        raise _form_changed("_field_value:507") from None
    if not isinstance(value, str):
        raise _form_changed("_field_value:509")
    return value


def _iso_date(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return value
    match = re.fullmatch(r"([0-9]{4})年([0-9]{2})月([0-9]{2})日", value)
    return "{}-{}-{}".format(*match.groups()) if match else None


def _hidden_name_selector(name: str) -> str:
    return f"input[type=\"hidden\"][name={json.dumps(name)}]"


def _milestone_form_contract(page: Any) -> Mapping[str, object]:
    amount = _one(page.locator('input[type="hidden"][name^="data[Milestone]["][name$="[amount_exclude_tax]"]')).nth(0)
    amount_name = amount.get_attribute("name")
    match = re.fullmatch(r"data\[Milestone\]\[([0-9]+)\]\[amount_exclude_tax\]", amount_name or "")
    if match is None:
        raise _form_changed("_milestone_form_contract:532")
    index = match.group(1)
    components = {}
    for component in ("year", "month", "day"):
        name = f"data[Milestone][{index}][schedule][{component}]"
        components[component] = _one(page.locator(_hidden_name_selector(name))).nth(0)
    return {"index": index, "amount": amount, "components": components}


def _wait_for_milestone_terms(
    page: Any, contract: Mapping[str, object], proposed_amount_minor: int, delivery_due_on: str,
) -> None:
    wait_for_function = getattr(page, "wait_for_function", None)
    if not callable(wait_for_function):
        raise _form_changed("_wait_for_milestone_terms:546")
    amount = contract["amount"]
    components = contract["components"]
    amount_name = amount.get_attribute("name")
    selectors = {key: _hidden_name_selector(control.get_attribute("name")) for key, control in components.items()}
    amount_selector = _hidden_name_selector(amount_name)
    year, month, day = delivery_due_on.split("-")
    script = (
        "() => {"
        f" const amount = document.querySelector({json.dumps(amount_selector)});"
        f" const year = document.querySelector({json.dumps(selectors['year'])});"
        f" const month = document.querySelector({json.dumps(selectors['month'])});"
        f" const day = document.querySelector({json.dumps(selectors['day'])});"
        f" return amount !== null && year !== null && month !== null && day !== null"
        f" && String(amount.value) === {json.dumps(str(proposed_amount_minor))}"
        f" && String(year.value) === {json.dumps(year)}"
        f" && String(month.value).padStart(2, '0') === {json.dumps(month)}"
        f" && String(day.value).padStart(2, '0') === {json.dumps(day)}; }}"
    )
    try:
        wait_for_function(script, timeout=5_000)
    except Exception:
        raise _form_changed("_wait_for_milestone_terms:568") from None
    if _field_value(amount) != str(proposed_amount_minor):
        raise _form_changed("_wait_for_milestone_terms:570")
    expected = {"year": year, "month": month, "day": day}
    for key, control in components.items():
        raw = _field_value(control)
        if key == "year" and raw != expected[key]:
            raise _form_changed("_wait_for_milestone_terms:575")
        if key != "year" and raw.zfill(2) != expected[key]:
            raise _form_changed("_wait_for_milestone_terms:577")


def _confirmation_terms(page: Any, project_id: str, milestone_index: str) -> Mapping[str, object]:
    form = _one(page.locator("form#ProposalProposeConfirmForm"))
    if (
        str(form.get_attribute("method") or "").upper() != "POST"
        or form.get_attribute("action") != f"/work/propose_finish/{project_id}"
    ):
        raise _form_changed("_confirmation_terms:586")
    row = _one(page.locator("tr.p-milestone-form__tr.Milestone"))
    amount = _one(row.locator(f'input#Milestone{milestone_index}AmountExcludeTax[type="hidden"]'))
    if (
        amount.get_attribute("id") != f"Milestone{milestone_index}AmountExcludeTax"
        or amount.get_attribute("type") != "hidden"
    ):
        raise _form_changed("_confirmation_terms:593")
    date_cell = _visible_one(row.locator("td.p-milestone-form__col.p-milestone-form__col--date"))
    raw_amount = _field_value(amount)
    raw_due = _iso_date(" ".join(date_cell.inner_text().split()))
    if re.fullmatch(r"[0-9]+", raw_amount) is None or raw_due is None:
        raise _form_changed("_confirmation_terms:598")
    return {"project_id": project_id, "amount_minor": int(raw_amount), "delivery_due_on": raw_due}


def _proposal_og_url(page: Any, expected: str) -> None:
    meta = _one(page.locator('meta[property="og:url"]')).nth(0)
    if meta.get_attribute("content") != expected:
        raise _form_changed("_proposal_og_url:605")


def _default_proposal_reader(page: Any, project_id: str) -> Mapping[str, object]:
    try:
        if not _route(getattr(page, "url", None), "/mypage/proposals"):
            return {}
        _one(page.locator(f'a[href="/work/detail/{project_id}"]'))
        own_view = _one(page.locator(
            f'a[href^="/work/proposals/{project_id}/"][href$="?ref=mypage_control"]'
        )).nth(0)
        if " ".join(own_view.inner_text().split()) != "提案をみる":
            return {}
        own_href = own_view.get_attribute("href")
        parsed_own = urlsplit(own_href or "")
        own_match = re.fullmatch(
            rf"/work/proposals/{re.escape(project_id)}/([^/?#\s]+)", parsed_own.path
        )
        if (
            parsed_own.scheme or parsed_own.netloc
            or parsed_own.query != "ref=mypage_control"
            or parsed_own.fragment
            or own_match is None
        ):
            return {}
        username = own_match.group(1)
        own_path = parsed_own.path
        page.goto(_url(own_path), wait_until="domcontentloaded", timeout=20_000)
        if not _route(getattr(page, "url", None), own_path):
            return {}
        own_url = _url(own_path)
        _proposal_og_url(page, own_url)

        heading = _one(page.locator("a.p-simpleProposal-list__heading-title")).nth(0)
        heading_text = " ".join(heading.inner_text().split())
        if re.fullmatch(r"\S.{0,99} さんの提案", heading_text) is None:
            return {}
        heading_href = heading.get_attribute("href")
        parsed_heading = urlsplit(heading_href or "")
        proposal_match = re.fullmatch(r"/work/proposal/([0-9]+)", parsed_heading.path)
        if (
            parsed_heading.scheme or parsed_heading.netloc or parsed_heading.query
            or parsed_heading.fragment or proposal_match is None
        ):
            return {}
        proposal_id = proposal_match.group(1)
        card = _one(page.locator(f"div#js-list-item-{proposal_id}")).nth(0)
        if card.get_attribute("id") != f"js-list-item-{proposal_id}":
            return {}
        card_heading = _one(card.locator("a.p-simpleProposal-list__heading-title")).nth(0)
        if (
            card_heading.get_attribute("href") != heading_href
            or " ".join(card_heading.inner_text().split()) != heading_text
        ):
            return {}

        return {"proposal_id": proposal_id, "project_id": project_id}
    except Exception:
        return {}


def _default_explicit_proposal_reader(
    page: Any, project_id: str, proposal_id: str,
) -> Mapping[str, object]:
    direct = f"/work/proposal/{proposal_id}"
    if not _route(getattr(page, "url", None), direct):
        return {}
    _proposal_og_url(page, _url(direct))
    project_link = _one(page.locator(f'a[href="/work/detail/{project_id}"]')).nth(0)
    if project_link.get_attribute("href") != f"/work/detail/{project_id}":
        return {}
    return {"proposal_id": proposal_id, "project_id": project_id}


def adopt_pending(
    *, project_id: str, proposal_id: str, state_path: Path = DEFAULT_STATE_PATH,
    browser_factory: Optional[Callable[[str], Any]] = None,
    ledger_writer: Optional[Callable[[Mapping[str, object]], object]] = None,
    now: Optional[Callable[[], object]] = None,
    proposal_reader: Callable[[Any, str, str], Mapping[str, object]] = _default_explicit_proposal_reader,
) -> TickResult:
    if (
        not isinstance(project_id, str) or re.fullmatch(r"[0-9]+", project_id) is None
        or not isinstance(proposal_id, str) or re.fullmatch(r"[0-9]+", proposal_id) is None
    ):
        return TickResult(ok=False, error="id_required", project_id=project_id if isinstance(project_id, str) else None)
    path = Path(state_path)
    marker = _application_marker(project_id)
    browser = page = None
    try:
        with account_lock(path):
            claims, pending = shared._read_state(path)
            if marker not in claims or marker not in pending:
                return TickResult(ok=True, reason="duplicate_project", project_id=project_id) if marker in claims else TickResult(ok=False, error="pending_not_found", project_id=project_id)
            pending_entry = pending[marker]
            if pending_entry.get("project_id") != project_id:
                return TickResult(ok=False, error="pending_project_mismatch", project_id=project_id)
            if pending_entry.get("proposal_id") is not None and pending_entry.get("proposal_id") != proposal_id:
                return TickResult(ok=False, error="pending_proposal_mismatch", project_id=project_id)
            try:
                browser, page = _open_owned_page(browser_factory)
            except Exception:
                return TickResult(ok=False, error="browser_unavailable", project_id=project_id)
            try:
                direct = f"/work/proposal/{proposal_id}"
                page.goto(_url(direct), wait_until="domcontentloaded", timeout=20_000)
                observed = _strict_identity(proposal_reader(page, project_id, proposal_id), project_id, proposal_id) if _route(getattr(page, "url", None), direct) else {}
            except Exception:
                observed = {}
            if not observed:
                return TickResult(ok=False, error="submission_uncertain", project_id=project_id, provider_proposal_id=proposal_id)
            try:
                writer = ledger_writer or _default_ledger_writer(path)
                clock = now or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
                return shared._reconcile_pending(
                    path=path, marker=marker, claims=claims, pending=pending,
                    pending_entry=dict(pending_entry), project_id=project_id, platform=PLATFORM,
                    readback=lambda _proposal, _project: {
                        **observed,
                        "amount_minor": pending_entry["amount_minor"],
                        "delivery_due_on": pending_entry["delivery_due_on"],
                    },
                    ledger_writer=writer, now=clock, submitted=False,
                )
            except Exception:
                return TickResult(ok=False, error="submission_uncertain", project_id=project_id, provider_proposal_id=proposal_id)
    except shared._AccountLockBusy:
        return TickResult(ok=False, error="account_lock_busy", project_id=project_id)
    except shared._StateInvalid:
        return TickResult(ok=False, error="state_invalid", project_id=project_id)
    finally:
        _close_owned_page(page)
        _stop_playwright_runtime(getattr(browser, "_anicca_playwright_runtime", None))


def _strict_identity(value: Any, project_id: str, proposal_id: Optional[str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        return {}
    found_id, found_project = value.get("proposal_id"), value.get("project_id")
    if not isinstance(found_id, str) or not found_id.strip() or found_project != project_id or (proposal_id is not None and found_id != proposal_id):
        return {}
    return {"proposal_id": found_id, "project_id": project_id}


def _strict_readback(value: Any, project_id: str, proposal_id: Optional[str]) -> Mapping[str, object]:
    identity = _strict_identity(value, project_id, proposal_id)
    if not identity:
        return {}
    amount, due = value.get("amount_minor"), _iso_date(value.get("delivery_due_on"))
    if (
        isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0 or due is None
    ):
        return {}
    return {**identity, "amount_minor": amount, "delivery_due_on": due}


def _production_submitter(
    page: Any, opportunity: Mapping[str, object], proposal_text: str,
    proposed_amount_minor: int, delivery_due_on: str,
    proposal_reader: Callable[[Any, str], Mapping[str, object]] = _default_proposal_reader,
) -> Mapping[str, object]:
    project_id = _project_id(opportunity)
    try:
        prepared = getattr(page, "_anicca_lancers_prepared", None)
        if not isinstance(prepared, Mapping) or prepared.get("project_id") != project_id:
            prepared = _production_prepare(
                page, project_id, proposed_amount_minor, delivery_due_on,
            )
        if not _route(
            getattr(page, "url", None),
            f"/work/propose_start/{project_id}",
            "proposeReferer=",
        ):
            raise _form_changed("_production_submitter:778")
        form = _one(page.locator("form#ProposalProposeForm"))
        if (
            str(form.get_attribute("method") or "").upper() != "POST"
            or form.get_attribute("action") != f"/work/propose_start/{project_id}"
        ):
            raise _form_changed("_production_submitter:784")
        body = _visible_one(form.locator('textarea#ProposalDescription[name="data[Proposal][description]"]'))
        amount = _visible_one(page.locator('#FeeApp input[type="number"][step="1000"][max="100000000"]'))
        due = _visible_one(page.locator('#FeeApp input[type="text"]'))
        ai_use = ai_use_label = None
        if _count(form.locator('input[name="data[ProposalAiDeclaration][ai_declaration]"]')):
            ai_use = _visible_one(form.locator('input#ProposalAiDeclarationAiDeclaration1[name="data[ProposalAiDeclaration][ai_declaration]"][value="1"][required]'))
            ai_use_label = _visible_one(form.locator('label[for="ProposalAiDeclarationAiDeclaration1"]'))
            if " ".join(ai_use_label.inner_text().split()) != "生成AIを使用している / 使用するが、著作権の侵害がなく、修正の要望も対応できる":
                raise _form_changed("_production_submitter:793")
        if not isinstance(proposal_text, str) or not proposal_text or not isinstance(proposed_amount_minor, int) or isinstance(proposed_amount_minor, bool) or proposed_amount_minor <= 0 or _iso_date(delivery_due_on) != delivery_due_on:
            raise RuntimeError("financial_terms_required")
        milestone = _milestone_form_contract(page)
        body.fill(proposal_text)
        if ai_use is not None and ai_use_label is not None:
            ai_use_label.click()
        if ai_use is not None and not ai_use.is_checked():
            raise _form_changed("_production_submitter:801")
        amount.fill(str(proposed_amount_minor))
        due.fill(delivery_due_on.replace("-", "年", 1).replace("-", "月", 1) + "日")
        for control in (amount, due):
            blur = getattr(control, "blur", None)
            if not callable(blur):
                raise _form_changed("_production_submitter:807")
            try:
                blur()
            except Exception:
                raise _form_changed("_production_submitter:811") from None
        _wait_for_milestone_terms(page, milestone, proposed_amount_minor, delivery_due_on)
        confirm = _visible_one(form.locator("#form_end"))
        confirm.click(no_wait_after=True)
        confirmation_url = _url(f"/work/propose_confirm/{project_id}")
        wait_for_url = getattr(page, "wait_for_url", None)
        if not callable(wait_for_url):
            raise _form_changed("_production_submitter:818")
        try:
            wait_for_url(confirmation_url, timeout=10_000)
        except Exception:
            raise _form_changed("_production_submitter:822") from None
        if not _route(getattr(page, "url", None), f"/work/propose_confirm/{project_id}"):
            raise _form_changed("_production_submitter:824")
        confirm_form = _one(page.locator("form#ProposalProposeConfirmForm"))
        if str(confirm_form.get_attribute("method") or "").upper() != "POST" or confirm_form.get_attribute("action") != f"/work/propose_finish/{project_id}":
            raise _form_changed("_production_submitter:827")
        terms = _confirmation_terms(page, project_id, str(milestone["index"]))
        if terms.get("project_id") != project_id or terms.get("amount_minor") != proposed_amount_minor or terms.get("delivery_due_on") != delivery_due_on:
            raise _form_changed("_production_submitter:830")
        final = _visible_one(confirm_form.locator('input#form_end[type="submit"][value="利用規約に同意して提案する"]'))
        if (
            final.get_attribute("id") != "form_end"
            or final.get_attribute("type") != "submit"
            or final.get_attribute("value") != "利用規約に同意して提案する"
        ):
            raise _form_changed("_production_submitter:837")
        try:
            if not final.is_enabled():
                raise _form_changed("_production_submitter:840")
        except RuntimeError:
            raise
        except Exception:
            raise _form_changed("_production_submitter:844") from None
    except SubmissionNotStarted:
        raise
    except RuntimeError as exc:
        if str(exc) in {"proposal_form_changed", "financial_terms_required"}:
            raise SubmissionNotStarted(str(exc)) from None
        raise
    expect_navigation = getattr(page, "expect_navigation", None)
    if not callable(expect_navigation):
        raise RuntimeError("submission_uncertain")
    try:
        with expect_navigation(wait_until="commit", timeout=10_000):
            final.click(no_wait_after=True)
    except Exception:
        raise RuntimeError("submission_uncertain") from None
    try:
        observed = _strict_identity(proposal_reader(page, project_id), project_id, None)
    except Exception:
        raise RuntimeError("submission_uncertain") from None
    return {**observed, "amount_minor": terms["amount_minor"], "delivery_due_on": terms["delivery_due_on"]} if observed else {}


def _production_readback(
    page: Any, proposal_id: Optional[str], project_id: str,
    proposal_reader: Callable[[Any, str], Mapping[str, object]] = _default_proposal_reader,
) -> Mapping[str, object]:
    route = "/mypage/proposals"
    page.goto(_url(route), wait_until="domcontentloaded", timeout=20_000)
    if not _route(getattr(page, "url", None), route):
        return {}
    return _strict_identity(proposal_reader(page, project_id), project_id, proposal_id)


def _default_ledger_writer(state_path: Path) -> Callable[[Mapping[str, object]], object]:
    ledger = Path(__file__).resolve().parents[3] / "_shared" / "marketplace-core" / "scripts" / "ledger.py"
    spec = importlib.util.spec_from_file_location("anicca_lancers_marketplace_ledger", ledger)
    if spec is None or spec.loader is None:
        raise RuntimeError("marketplace_ledger_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    database = Path(state_path).with_name("marketplace-ledger.sqlite3")
    return lambda record: module.append_event(database, record)


def _application_marker(project_id: str) -> str:
    return hashlib.sha256(f"{PLATFORM}:application:{project_id}".encode()).hexdigest()


def _state_has_claim(state_path: Path, project_id: str) -> bool:
    """Read only the local marker needed to choose preflight vs reconciliation."""
    try:
        value = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return False
    if not isinstance(value, Mapping):
        return False
    fingerprints = value.get("fingerprints")
    return isinstance(fingerprints, list) and _application_marker(project_id) in fingerprints


def state_has_claim(state_path: Path, project_id: str) -> bool:
    """Read the strict transaction state for the pre-planner claim filter."""
    fingerprints, _pending = shared._read_state(Path(state_path))
    return _application_marker(project_id) in fingerprints


def run_live_tick(
    *, project_id: str, proposal_text: str, proposed_amount_minor: int, delivery_due_on: str,
    title: Optional[str] = None,
    state_path: Path = DEFAULT_STATE_PATH, browser_factory: Optional[Callable[[str], Any]] = None,
    ledger_writer: Optional[Callable[[Mapping[str, object]], object]] = None,
    now: Optional[Callable[[], object]] = None,
    submitter_override: Optional[Callable[..., Mapping[str, object]]] = None,
    readback_override: Optional[Callable[..., Mapping[str, object]]] = None,
    proposal_reader: Callable[[Any, str], Mapping[str, object]] = _default_proposal_reader,
) -> Any:
    state_path = Path(state_path)
    try:
        if _state_has_terminal_block(state_path, str(project_id)):
            return TickResult(ok=False, error=TERMINAL_STATE_STATUS, project_id=str(project_id))
    except RuntimeError:
        return TickResult(ok=False, error="state_invalid", project_id=str(project_id))
    browser = page = None
    try:
        with account_lock(state_path.with_name("work-sync.json")):
            try:
                browser, page = _open_owned_page(browser_factory)
            except Exception:
                _stop_playwright_runtime(getattr(browser, "_anicca_playwright_runtime", None))
                return TickResult(ok=False, error="browser_unavailable", project_id=str(project_id))
            if not _production_account_ready(page):
                return TickResult(ok=False, error="account_unavailable", project_id=str(project_id))
            pending_claim = _state_has_claim(state_path, str(project_id))
            pending_terms = next(
            (
                descriptor
                for descriptor in read_pending_descriptors(state_path)
                if isinstance(descriptor, Mapping)
                and descriptor.get("project_id") == str(project_id)
            ),
            {},
            ) if pending_claim else {}
            if not pending_claim:
                try:
                    _production_prepare(page, str(project_id), proposed_amount_minor, delivery_due_on)
                except RuntimeError as exc:
                    error = str(exc) or "proposal_form_changed"
                    if error == TERMINAL_STATE_STATUS:
                        try:
                            _record_terminal_block(
                                state_path, str(project_id),
                                now or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
                            )
                        except Exception:
                            return TickResult(ok=False, error="state_invalid", project_id=str(project_id))
                        return TickResult(ok=False, error=TERMINAL_STATE_STATUS, project_id=str(project_id))
                    if error not in {"financial_terms_required", "proposal_form_changed"}:
                        # Relabelling is kept -- the caller's contract is a small set of codes --
                        # but the real reason is written down first. Measured 2026-09-07: the lane
                        # reported proposal_form_changed while proposal-form-changes.jsonl stayed
                        # empty, because these two handlers rename anything that reaches them and
                        # the named recorder below only ever sees failures raised by _form_changed.
                        _record_form_change(f"_production_prepare:relabelled:{error[:60]}",
                                            "relabelled_as_form_changed",
                                            {"project_id": str(project_id), "original": error[:200]})
                        error = "proposal_form_changed"
                    return TickResult(ok=False, error=error, project_id=str(project_id))
                except Exception as exc:
                    # Any exception at all became proposal_form_changed here -- a browser timeout,
                    # a dead page, a bug of mine -- so the code named the step that had least to do
                    # with it. The exception type is what tells them apart.
                    _record_form_change(f"_production_prepare:unhandled:{type(exc).__name__}",
                                        "unhandled_exception",
                                        {"project_id": str(project_id), "detail": str(exc)[:200]})
                    return TickResult(ok=False, error="proposal_form_changed", project_id=str(project_id))
            submitter = submitter_override or (lambda opportunity, text, amount, due: _production_submitter(page, opportunity, text, amount, due, proposal_reader))

            def readback(proposal: Optional[str], project: str) -> Mapping[str, object]:
                browser_value = readback_override(proposal, project) if readback_override is not None else _production_readback(page, proposal, project, proposal_reader)
                identity = _strict_identity(browser_value, project, proposal)
                terms = pending_terms if pending_claim else {
                    "project_id": project, "amount_minor": proposed_amount_minor,
                    "delivery_due_on": delivery_due_on,
                }
                return _strict_readback({**identity, **terms}, project, proposal) if identity and isinstance(terms, Mapping) and terms.get("project_id") == project else {}

            return run_tick(
                # The title has to travel with the claim. run_tick stores it on the pending entry
                # so the reconcile that confirms the application -- which knows only ids -- can
                # still name the job. Without it every confirmed application reported 「案件<id>」.
                opportunity={"external_id": str(project_id), "platform": PLATFORM,
                             **({"title": title.strip()[:300]} if isinstance(title, str) and title.strip() else {})},
                proposal_text=proposal_text,
                proposed_amount_minor=proposed_amount_minor, delivery_due_on=delivery_due_on,
                state_path=state_path, account_ready=lambda: True,
                submitter=submitter, readback=readback,
                ledger_writer=ledger_writer or _default_ledger_writer(state_path),
                now=now or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")),
            )
    except shared._AccountLockBusy:
        return TickResult(ok=False, error="account_lock_busy", project_id=str(project_id))
    finally:
        _close_owned_page(page)
        _stop_playwright_runtime(getattr(browser, "_anicca_playwright_runtime", None))


def main(
    argv: Optional[Sequence[str]] = None, *, input_stream: Any = None, output_stream: Any = None,
    browser_factory: Optional[Callable[[str], Any]] = None,
    ledger_writer: Optional[Callable[[Mapping[str, object]], object]] = None,
    now: Optional[Callable[[], object]] = None,
    proposal_reader: Callable[[Any, str, str], Mapping[str, object]] = _default_explicit_proposal_reader,
) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit", allow_abbrev=False)
    submit.add_argument("--project-id", required=True)
    submit.add_argument("--proposed-amount-minor", required=True, type=int)
    submit.add_argument("--delivery-due-on", required=True)
    submit.add_argument("--proposal-stdin", action="store_true", required=True)
    submit.add_argument("--json", action="store_true", required=True)
    submit.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    adopt = sub.add_parser("adopt-pending", allow_abbrev=False)
    adopt.add_argument("--project-id", required=True)
    adopt.add_argument("--proposal-id", required=True)
    adopt.add_argument("--json", action="store_true")
    adopt.add_argument("--state-path", default=str(DEFAULT_STATE_PATH))
    args = parser.parse_args(argv)
    output = output_stream or sys.stdout
    if args.command == "adopt-pending":
        result = adopt_pending(
            project_id=args.project_id, proposal_id=args.proposal_id,
            state_path=Path(args.state_path), browser_factory=browser_factory,
            ledger_writer=ledger_writer, now=now, proposal_reader=proposal_reader,
        )
    else:
        stream = input_stream or sys.stdin
        try:
            proposal_text = stream.read().rstrip("\r\n")
        except Exception:
            proposal_text = ""
        result = TickResult(ok=False, error="proposal_text_required", project_id=args.project_id) if not proposal_text else run_live_tick(
            project_id=args.project_id, proposal_text=proposal_text, proposed_amount_minor=args.proposed_amount_minor,
            delivery_due_on=args.delivery_due_on, state_path=Path(args.state_path), browser_factory=browser_factory,
            ledger_writer=ledger_writer, now=now,
        )
    output.write(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()
    return 0 if result.ok else 1


__all__ = ["CDP_URL", "DEFAULT_STATE_PATH", "SubmissionNotStarted", "TickResult", "account_lock", "load_marketplace_contracts", "read_pending_descriptor", "state_has_claim", "run_tick", "run_live_tick", "adopt_pending", "main", "_default_browser_factory", "_new_owned_page", "_close_owned_page", "_production_account_ready", "_production_prepare", "_production_submitter", "_production_readback", "_default_explicit_proposal_reader"]


if __name__ == "__main__":
    raise SystemExit(main())
