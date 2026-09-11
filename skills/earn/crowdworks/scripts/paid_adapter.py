#!/usr/bin/env python3
"""Thin CrowdWorks boundary for the shared marketplace Paid kernel."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


HERE = Path(__file__).resolve().parent
SHARED = HERE.parents[2] / "_shared/marketplace-core/scripts"
ACTIVE_CONTRACTS_URL = "https://crowdworks.jp/e/contracts?status=active"
ACCOUNT_ID = "7145638"
DEFAULT_APPLICATION_RECEIPTS = Path.home() / ".local/state/anicca/crowdworks/application-receipts.jsonl"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


account = _load("crowdworks_paid_account", HERE / "account.py")
composer = _load("crowdworks_paid_composer", SHARED / "reply_composer.py")
grounding_module = _load("crowdworks_paid_grounding", SHARED / "reply_grounding.py")
profile_module = _load("crowdworks_paid_profile", HERE / "profile.py")
google_form = _load("crowdworks_paid_google_form", HERE / "google_form.py")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any, error: str = "crowdworks_paid_invalid") -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise RuntimeError(error)
    return str(value).strip()


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(value), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _google_form_url(value: str) -> bool:
    return google_form.is_google_form_url(value)


def _connect_existing_cdp() -> tuple[Any, Any]:
    """Connect only; never create, repair, or close the CrowdWorks browser process."""
    runtime = sync_playwright().start()
    try:
        return runtime, runtime.chromium.connect_over_cdp(account.CDP_URL)
    except Exception:
        runtime.stop()
        raise RuntimeError("crowdworks_paid_browser_unavailable") from None


class CrowdWorksPaidWait(RuntimeError):
    def __init__(self, reason: str, remaining_work: list[str]):
        super().__init__(reason)
        self.paid_wait_reason = reason
        self.paid_remaining_work = remaining_work


class CrowdWorksPaidActiveContractsTimeout(RuntimeError):
    pass


class CrowdWorksPaidContractTimeout(RuntimeError):
    pass


class CrowdWorksPaidProposalTimeout(RuntimeError):
    pass


class CrowdWorksPaidMilestoneTimeout(RuntimeError):
    pass


_TIMEOUTS = {
    "active_contracts": CrowdWorksPaidActiveContractsTimeout,
    "contract": CrowdWorksPaidContractTimeout,
    "proposal": CrowdWorksPaidProposalTimeout,
    "milestone": CrowdWorksPaidMilestoneTimeout,
}


class CrowdWorksPaidAdapter:
    """CrowdWorks-only observation and effects; kernel owns durable lifecycle."""

    def __init__(self, *, account_id: str, inventory_reader: Callable[[], Mapping[str, Any]] | None = None,
                 state_path: Path | None = None, candidate_profile: Path | None = None,
                 provider_profile: Mapping[str, Any] | None = None,
                 connection_factory: Callable[[], tuple[Any, Any]] | None = None,
                 application_receipts_path: Path = DEFAULT_APPLICATION_RECEIPTS):
        if not isinstance(account_id, str) or not account_id.strip():
            raise ValueError("crowdworks_account_id_invalid")
        self.account_id = account_id.strip()
        self.inventory_reader = inventory_reader
        self.state_path = Path(state_path) if state_path is not None else None
        self.candidate_profile = Path(candidate_profile) if candidate_profile is not None else None
        self.provider_profile = dict(provider_profile) if provider_profile is not None else None
        self.connection_factory = connection_factory or _connect_existing_cdp
        self.application_receipts_path = Path(application_receipts_path).expanduser()
        self._local = threading.local()
        self._cache_lock = threading.Lock()
        # Contract facts only: never a Playwright runtime, browser, context, or page.
        self._contract_cache: dict[str, dict[str, Any]] = {}

    @property
    def browser(self):
        return getattr(self._local, "browser", None)

    @browser.setter
    def browser(self, value) -> None:
        self._local.browser = value

    @property
    def page(self):
        return getattr(self._local, "page", None)

    @page.setter
    def page(self, value) -> None:
        self._local.page = value

    @property
    def runtime(self):
        return getattr(self._local, "runtime", None)

    @runtime.setter
    def runtime(self, value) -> None:
        self._local.runtime = value

    def _open(self) -> None:
        if self.page is not None:
            return
        self.runtime, self.browser = self.connection_factory()
        contexts = getattr(self.browser, "contexts", ())
        if len(contexts) != 1:
            self.runtime.stop()
            self.runtime = self.browser = None
            raise RuntimeError("crowdworks_paid_browser_unavailable")
        self.page = contexts[0].new_page()
        self.page.set_default_timeout(15_000)

    @staticmethod
    def _goto(page: Any, url: str, stage: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeoutError:
            raise _TIMEOUTS[stage]() from None

    def _goto_contract(self, work_id: str) -> None:
        self._open()
        if not re.fullmatch(r"\d+", work_id):
            raise RuntimeError("crowdworks_paid_work_invalid")
        self._goto(self.page, f"https://crowdworks.jp/contracts/{work_id}", "contract")
        parsed = urlsplit(str(self.page.url))
        if (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment) != (
                "https", "crowdworks.jp", f"/contracts/{work_id}", "", ""):
            raise RuntimeError("crowdworks_paid_contract_unavailable")

    @staticmethod
    def _row_from_list(raw: Mapping[str, Any]) -> dict[str, str]:
        work_id, title, client, status = (_text(raw.get(key)) for key in
                                          ("work_id", "title", "client", "provider_state"))
        if status not in {"funded", "awaiting_escrow", "delivered"}:
            raise RuntimeError("crowdworks_paid_contract_state_invalid")
        result: dict[str, Any] = {"work_id": work_id, "title": title, "client": client,
                                  "provider_state": status}
        for key in ("milestone_id", "form_url", "application_date"):
            value = raw.get(key)
            if value is not None:
                result[key] = _text(value)
        return result

    def _list_contracts(self) -> list[dict[str, str]]:
        self._open()
        self._goto(self.page, ACTIVE_CONTRACTS_URL, "active_contracts")
        parsed = urlsplit(str(self.page.url))
        if ((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
                != ("https", "crowdworks.jp", "/e/contracts", "status=active", "")):
            raise RuntimeError("crowdworks_paid_contract_source_unavailable")
        values = self.page.locator('a[href^="/contracts/"]').evaluate_all(
            """nodes => nodes.map(a => { const row=a.closest('tr'); return {
              href:a.getAttribute('href'), title:a.innerText.trim(), row:row?.innerText.trim() || ''
            }}).filter(x => x.href && x.title && x.row)"""
        )
        if not isinstance(values, list):
            raise RuntimeError("crowdworks_paid_contract_source_unavailable")
        result: list[dict[str, str]] = []
        for value in values:
            if not isinstance(value, Mapping):
                raise RuntimeError("crowdworks_paid_contract_source_unavailable")
            match = re.fullmatch(r"/contracts/(\d+)", str(value.get("href") or ""))
            row = _text(value.get("row"), "crowdworks_paid_contract_source_unavailable")
            if match is None:
                raise RuntimeError("crowdworks_paid_contract_source_unavailable")
            state = ("funded" if "進行中" in row else "awaiting_escrow" if "仮払い待ち" in row
                     else "delivered" if "検収中" in row or "納品済み" in row else "")
            parts = [part.strip() for part in re.split(r"[\t\r\n]+", row) if part.strip()]
            if not state or len(parts) < 3:
                raise RuntimeError("crowdworks_paid_contract_state_invalid")
            result.append({"work_id": match.group(1), "title": _text(value.get("title")),
                           "client": parts[0], "provider_state": state})
        if len({row["work_id"] for row in result}) != len(result):
            raise RuntimeError("crowdworks_paid_contract_duplicate")
        return result

    def _detail(self, basic: Mapping[str, Any]) -> dict[str, Any]:
        work_id = _text(basic.get("work_id"))
        self._goto_contract(work_id)
        body = _text(self.page.locator("body").inner_text(), "crowdworks_paid_contract_unavailable")
        title, client = (_text(basic.get(key)) for key in ("title", "client"))
        if title not in body or client not in body:
            raise RuntimeError("crowdworks_paid_contract_context_invalid")
        state = ("funded" if "業務を開始しています" in body else
                 "awaiting_escrow" if "仮払いを行っています" in body and "業務を開始しない" in body else
                 "delivered" if any(token in body for token in ("検収", "納品済み", "納品完了")) else "")
        if not state:
            raise RuntimeError("crowdworks_paid_contract_state_changed")
        if state == "awaiting_escrow":
            return {"work_id": work_id, "title": title, "client": client, "provider_state": state,
                    "milestone_id": None, "form_url": None, "proposal_id": None,
                    "application_date": None}
        if state == "delivered":
            forms = self.page.locator('form[action^="/milestones/"][action$="/complete"]')
            if forms.count() or not any(token in body for token in ("検収", "納品済み", "納品完了")):
                raise RuntimeError("crowdworks_paid_contract_state_changed")
            return {"work_id": work_id, "title": title, "client": client, "provider_state": state,
                    "milestone_id": None, "form_url": None, "proposal_id": None,
                    "application_date": None}
        forms = self.page.locator('form[action^="/milestones/"][action$="/complete"]')
        actions = {str(forms.nth(i).get_attribute("action") or "") for i in range(forms.count())}
        match = re.fullmatch(r"/milestones/(\d+)/complete", actions.pop()) if len(actions) == 1 else None
        proposal_ids = sorted({found.group(1) for href in self.page.locator('a[href]').evaluate_all(
            "nodes => nodes.map(a => a.getAttribute('href')).filter(Boolean)") if isinstance(href, str)
                               for found in [re.match(r"^/proposals/(\d+)(?:/|$)", href)] if found})
        proposal_id = proposal_ids[0] if len(proposal_ids) == 1 else None
        application_date = basic.get("application_date") if basic.get("proposal_id") == proposal_id else None
        if application_date is None and proposal_id is not None:
            application_date = self._proposal_application_date(proposal_id)
        if application_date is None and proposal_id is not None:
            application_date = self._receipt_application_date(title, proposal_id)
        links = self.page.locator('a[href]').evaluate_all("nodes => nodes.map(a => a.href).filter(Boolean)")
        form_urls = sorted({link for link in links if isinstance(link, str) and _google_form_url(link)})
        if match is None or len(form_urls) != 1:
            raise RuntimeError("crowdworks_paid_task_unavailable")
        return {"work_id": work_id, "title": title, "client": client, "provider_state": state,
                "milestone_id": match.group(1), "form_url": form_urls[0],
                "proposal_id": proposal_id, "application_date": application_date}

    def _proposal_application_date(self, proposal_id: str) -> str | None:
        proposal = self.browser.contexts[0].new_page()
        try:
            try:
                self._goto(proposal, f"https://crowdworks.jp/proposals/{proposal_id}", "proposal")
            except CrowdWorksPaidProposalTimeout:
                return None
            route = urlsplit(str(proposal.url))
            if (route.scheme, route.netloc, route.path, route.query, route.fragment) != (
                    "https", "crowdworks.jp", f"/proposals/{proposal_id}", "", ""):
                return None
            body = _text(proposal.locator("body").inner_text(), "crowdworks_paid_proposal_unavailable")
            matched = re.search(r"(?:応募日時|応募日)\s*[：:]?\s*(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})", body)
            if matched is None:
                return None
            try:
                return datetime(*(int(value) for value in matched.groups())).date().isoformat()
            except ValueError:
                return None
        finally:
            proposal.close()

    def _receipt_application_date(self, title: str, proposal_id: str) -> str | None:
        """Use only one exact verified official Apply receipt as a proposal-date fallback."""
        matches = []
        try:
            lines = self.application_receipts_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None
        except OSError:
            raise RuntimeError("crowdworks_paid_application_receipt_unavailable") from None
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                raise RuntimeError("crowdworks_paid_application_receipt_invalid") from None
            if not isinstance(row, Mapping):
                raise RuntimeError("crowdworks_paid_application_receipt_invalid")
            if (row.get("record_type") != "application_receipt" or row.get("platform") != "crowdworks"
                    or row.get("status") != "verified" or row.get("opportunity_title") != title
                    or row.get("application_external_id") != proposal_id):
                continue
            opportunity_id = row.get("opportunity_external_id")
            observed_at = row.get("observed_at")
            if (not isinstance(opportunity_id, str) or not re.fullmatch(r"\d+", opportunity_id)
                    or not isinstance(observed_at, str)):
                raise RuntimeError("crowdworks_paid_application_receipt_invalid")
            try:
                stamp = datetime.fromisoformat(observed_at)
                if stamp.tzinfo is None:
                    raise ValueError
            except ValueError:
                raise RuntimeError("crowdworks_paid_application_receipt_invalid") from None
            matches.append(stamp.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat())
        return matches[0] if len(matches) == 1 else None

    def _observation(self, item: Mapping[str, Any]) -> dict[str, str]:
        stable = {key: item.get(key) for key in ("work_id", "title", "client", "provider_state",
                                                  "milestone_id", "form_url", "proposal_id", "application_date")}
        return {"provider": "crowdworks", "account_id": self.account_id,
                "work_id": _text(item.get("work_id")), "latest_event_id": _digest(stable),
                "provider_state": _text(item.get("provider_state")), "observed_at": _now()}

    def _cache_replace(self, items: list[Mapping[str, Any]]) -> None:
        snapshot = { _text(item.get("work_id")): dict(item) for item in items }
        with self._cache_lock:
            self._contract_cache = snapshot

    def _cache_update(self, item: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(item); work_id = _text(normalized.get("work_id"))
        with self._cache_lock:
            self._contract_cache[work_id] = normalized
        return dict(normalized)

    def _cached_item(self, work_id: str) -> dict[str, Any] | None:
        with self._cache_lock:
            value = self._contract_cache.get(work_id)
            return dict(value) if value is not None else None

    def _inventory_rows(self) -> list[dict[str, Any]]:
        if self.inventory_reader is None:
            return self._list_contracts()
        snapshot = self.inventory_reader()
        if (not isinstance(snapshot, Mapping) or snapshot.get("ok") is not True
                or snapshot.get("source_complete") is not True
                or not isinstance(snapshot.get("contract_candidates"), list)):
            raise RuntimeError("crowdworks_paid_inventory_unavailable")
        details = [self._row_from_list(row) for row in snapshot["contract_candidates"] if isinstance(row, Mapping)]
        if len(details) != len(snapshot["contract_candidates"]):
            raise RuntimeError("crowdworks_paid_inventory_unavailable")
        return details

    def _inventory(self) -> list[dict[str, Any]]:
        rows = self._inventory_rows()
        details = rows if self.inventory_reader is not None else [self._detail(row) for row in rows]
        self._cache_replace(details)
        return [self._observation(item) for item in details]

    def _targeted_detail(self, work_id: str) -> dict[str, Any]:
        """Refresh exactly one contract, reusing only immutable pure data as a hint."""
        basic = self._cached_item(work_id)
        if self.inventory_reader is not None:
            matches = [item for item in self._inventory_rows() if item["work_id"] == work_id]
            if len(matches) != 1:
                raise RuntimeError("crowdworks_paid_work_unavailable")
            return self._cache_update(matches[0])
        if basic is None:
            matches = [item for item in self._list_contracts() if item["work_id"] == work_id]
            if len(matches) != 1:
                raise RuntimeError("crowdworks_paid_work_unavailable")
            basic = matches[0]
        return self._cache_update(self._detail(basic))

    def observe_active(self) -> list[dict[str, Any]]:
        try:
            return self._inventory()
        finally:
            # Worker threads independently re-observe their work item; the
            # inventory thread never lends its CDP page to them.
            self.close()

    def observe_one(self, work_id: str) -> dict[str, Any]:
        try:
            return self._observation(self._targeted_detail(work_id))
        finally:
            self.close()

    def context(self, work_id: str) -> dict[str, Any]:
        try:
            item = self._cached_item(work_id) or self._targeted_detail(work_id)
            return {"contract": dict(item), "delivery": {
                "formal_delivery_authorized": item["provider_state"] == "funded",
                "form_required": bool(item.get("form_url")),
            }}
        finally:
            self.close()

    def _receipt_path(self, form_sha256: str) -> Path:
        if self.state_path is None:
            raise RuntimeError("crowdworks_paid_state_unavailable")
        return google_form.receipt_path(self.state_path, form_sha256)

    def _form_items(self, page: Any) -> list[dict[str, Any]]:
        raw = page.evaluate("window.FB_PUBLIC_LOAD_DATA_ && window.FB_PUBLIC_LOAD_DATA_[1][1]")
        if not isinstance(raw, list):
            raise RuntimeError("crowdworks_paid_form_metadata_invalid")
        items = []
        for value in raw:
            if not isinstance(value, list) or len(value) < 5 or not isinstance(value[1], str):
                continue
            entries = []
            for entry in value[4] if isinstance(value[4], list) else []:
                if not isinstance(entry, list) or not entry or not isinstance(entry[0], int):
                    continue
                choices = [choice[0] for choice in entry[1] if isinstance(choice, list) and choice and isinstance(choice[0], str)] if len(entry) > 1 and isinstance(entry[1], list) else []
                entries.append({"id": entry[0], "required": bool(entry[2]) if len(entry) > 2 else False, "choices": choices})
            if entries:
                items.append({"title": value[1].strip(), "type": value[3], "entries": entries})
        if not items:
            raise RuntimeError("crowdworks_paid_form_metadata_invalid")
        return items

    def _compose_text(self, *, question: str, source: str, item: Mapping[str, Any],
                      choices: list[str] | None = None) -> str:
        if self.candidate_profile is None or self.provider_profile is None or self.state_path is None:
            raise RuntimeError("crowdworks_paid_form_profile_unavailable")
        grounding = grounding_module.build_reply_grounding(candidate_profile_path=self.candidate_profile,
                                                           provider_profile=self.provider_profile)
        body = composer.compose({"board": {"title": item["title"]}, "grounding": grounding,
            "conversation": [{"role": "buyer", "body": "契約済み業務のGoogleフォームに記載する文章を作成してください。設問: " + question + "\n業務説明: " + source}],
            "action_contract": {"kind": "required_form_field", "question": question,
                                "allowed_choices": list(choices or [])},
            "provider_rules": {"outside_contact_before_approval": "forbidden"}},
            state_root=self.state_path / "compose", task_label="crowdworks-paid-form")
        if not isinstance(body, str) or not body.strip():
            raise RuntimeError("crowdworks_paid_form_composition_unavailable")
        value = body.strip()
        if choices and value not in choices:
            raise RuntimeError("crowdworks_paid_form_choice_invalid")
        return value

    def _form_fields(self, page: Any, item: Mapping[str, Any]) -> list[tuple[str, str]]:
        started = time.monotonic()
        source = _text(page.locator("body").inner_text(), "crowdworks_paid_form_unavailable")
        provider = self.provider_profile or {}
        result: list[tuple[str, str]] = []
        for question in self._form_items(page):
            entry = question["entries"][0]
            if not entry["required"]:
                continue
            title, choices = question["title"], entry["choices"]
            if choices:
                value = choices[0] if len(choices) == 1 else self._compose_text(
                    question=title, source=source, item=item, choices=choices)
            elif "登録名" in title:
                value = _text(provider.get("display_name"), "crowdworks_paid_form_profile_unavailable")
            elif "やり取り" in title and "リンク" in title:
                value = f"https://crowdworks.jp/contracts/{item['work_id']}"
            elif question["type"] == 9:
                try:
                    stamp = datetime.fromisoformat(_text(item.get("application_date"))).date()
                except ValueError:
                    raise RuntimeError("crowdworks_paid_form_date_unknown") from None
                result.extend([(f"entry.{entry['id']}_year", str(stamp.year)), (f"entry.{entry['id']}_month", str(stamp.month)), (f"entry.{entry['id']}_day", str(stamp.day))])
                continue
            elif "時間" in title and "分" in title:
                value = str(math.ceil((time.monotonic() - started) / 60))
            else:
                value = self._compose_text(question=title, source=source, item=item)
            result.append((f"entry.{entry['id']}", value))
        return result

    def _submit_form_once(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        form_url = _text(item.get("form_url")); form_sha256 = hashlib.sha256(form_url.encode()).hexdigest()
        if self.state_path is None:
            raise RuntimeError("crowdworks_paid_state_unavailable")
        return google_form.submit_once(
            browser=self.browser, state_root=self.state_path, url=form_url, url_sha256=form_sha256,
            answer_fields=lambda page: self._form_fields(page, item),
            binding=self._form_binding(item, form_sha256),
        )

    def _form_binding(self, item: Mapping[str, Any], form_sha256: str) -> dict[str, str]:
        return {"provider": "crowdworks", "account_id": self.account_id,
                "contract_id": _text(item.get("work_id")), "milestone_id": _text(item.get("milestone_id")),
                "form_revision_sha256": form_sha256}

    def _complete_once(self, item: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
        self._goto_contract(_text(item.get("work_id")))
        selector = f'form[action="/milestones/{_text(payload.get("milestone_id"))}/complete"]'
        def visible_forms():
            forms = self.page.locator(selector)
            return [forms.nth(index) for index in range(forms.count())
                    if forms.nth(index).locator('textarea[name="message[body]"]').is_visible()]

        visible = visible_forms()
        if not visible:
            tabs = self.page.get_by_text("やること", exact=True)
            visible_tabs = [tabs.nth(index) for index in range(tabs.count()) if tabs.nth(index).is_visible()]
            if len(visible_tabs) != 1:
                raise RuntimeError("crowdworks_paid_todo_surface_unavailable")
            visible_tabs[0].click()
            self.page.locator(f'{selector} textarea[name="message[body]"]:visible').wait_for(
                state="visible", timeout=15_000)
            visible = visible_forms()
        if len(visible) != 1:
            raise RuntimeError("crowdworks_paid_milestone_unavailable")
        form = visible[0]
        form.locator('textarea[name="message[body]"]').fill(
            "Googleフォームへの回答を完了しました。ご確認のほどよろしくお願いいたします。")
        # The official form has duplicate milestone forms in the DOM.  Fence the
        # effect to the selected milestone's named submit control, rather than a
        # same-looking generic submit input.
        submit = form.locator('input[name="commit"][type="submit"][value="納品完了報告をする"]')
        if submit.count() != 1 or submit.is_disabled():
            raise RuntimeError("crowdworks_paid_milestone_submit_unavailable")
        try:
            submit.click()
            self.page.wait_for_load_state("domcontentloaded", timeout=20_000)
        except PlaywrightTimeoutError:
            raise CrowdWorksPaidMilestoneTimeout() from None

    def mutate(self, intent: dict[str, Any]) -> None:
        try:
            if intent.get("action") != "submit" or not isinstance(intent.get("payload"), Mapping):
                raise RuntimeError("crowdworks_paid_effect_unsupported")
            payload = intent["payload"]
            work_id = _text(intent.get("work_id"))
            current = self._targeted_detail(work_id)
            url = current.get("form_url")
            if (current.get("provider_state") != "funded" or payload.get("milestone_id") != current.get("milestone_id")
                    or payload.get("form_url") != url or payload.get("form_sha256") != hashlib.sha256(str(url).encode()).hexdigest()):
                raise RuntimeError("crowdworks_paid_context_changed")
            self._submit_form_once(current)
            self._complete_once(current, payload)
        finally:
            self.close()

    def readback(self, intent: dict[str, Any]) -> dict[str, Any]:
        try:
            if intent.get("action") != "submit" or not isinstance(intent.get("payload"), Mapping):
                return {"authoritative_absent": True}
            payload = intent["payload"]; work_id = _text(intent.get("work_id")); self._goto_contract(work_id)
            form_sha256 = _text(payload.get("form_sha256"))
            binding = {"provider": "crowdworks", "account_id": self.account_id, "contract_id": work_id,
                       "milestone_id": _text(payload.get("milestone_id")), "form_revision_sha256": form_sha256}
            receipt = google_form.bound_receipt(self.state_path, binding)
            form_done = isinstance(receipt, Mapping) and receipt.get("url_sha256") == form_sha256 and bool(receipt.get("confirmation_sha256"))
            actions = self.page.locator('form[action^="/milestones/"][action$="/complete"]').evaluate_all("forms => forms.map(form => form.getAttribute('action'))")
            milestone_id = _text(payload.get("milestone_id"))
            body = _text(self.page.locator("body").inner_text(), "crowdworks_paid_contract_unavailable")
            delivered = (f"/milestones/{milestone_id}/complete" not in actions
                         and any(token in body for token in ("検収", "納品済み", "納品完了")))
            if form_done and delivered:
                return {"verified": True, "provider_receipt_id": f"contract:{work_id}:milestone:{milestone_id}", "observed_at": _now()}
            # A confirmed form receipt fences a second Form POST.  The still-visible
            # CrowdWorks milestone form is authoritative evidence that the separate,
            # reversible completion step has not happened and may be resumed.
            return {"authoritative_absent": True}
        finally:
            self.close()

    def close(self) -> None:
        if self.page is not None:
            try: self.page.close()
            except Exception: pass
        self.page = self.browser = None
        if self.runtime is not None:
            try: self.runtime.stop()
            except Exception: pass
        self.runtime = None


def read_only_inventory() -> dict[str, Any]:
    adapter = CrowdWorksPaidAdapter(account_id=ACCOUNT_ID)
    try:
        rows = adapter.observe_active()
        return {"ok": True, "source_complete": True, "contract_candidates": [
            {"provider_id": row["work_id"], "provider_state": row["provider_state"]} for row in rows], "observed_at": _now()}
    finally:
        adapter.close()


def decide(row: Mapping[str, Any]) -> dict[str, Any]:
    context = row.get("context"); contract = context.get("contract") if isinstance(context, Mapping) else None
    if not isinstance(contract, Mapping):
        raise RuntimeError("crowdworks_paid_context_unavailable")
    if contract.get("provider_state") == "awaiting_escrow":
        return {"action": "wait", "reason": "awaiting_client_escrow", "remaining_work": ["wait for official CrowdWorks escrow completion before beginning work"]}
    if contract.get("provider_state") == "delivered":
        return {"action": "noop", "classification": "completed"}
    if not isinstance(contract.get("application_date"), str):
        return {"action": "wait", "reason": "official_application_date_required",
                "remaining_work": ["locate a labeled official CrowdWorks proposal application date before form submission"]}
    form_url, milestone_id = contract.get("form_url"), contract.get("milestone_id")
    if contract.get("provider_state") != "funded" or not isinstance(form_url, str) or not _google_form_url(form_url) or not isinstance(milestone_id, str):
        return {"action": "wait", "reason": "buyer_task_detail_required", "remaining_work": ["read the official funded contract task before any delivery effect"]}
    return {"action": "submit", "payload": {"form_url": form_url, "form_sha256": hashlib.sha256(form_url.encode()).hexdigest(), "milestone_id": milestone_id}}


def build(argv: list[str]):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True); parser.add_argument("--state-path", required=True, type=Path)
    parser.add_argument("--candidate-profile", type=Path, default=Path.home() / ".config/anicca/job-search/profile.json")
    parser.add_argument("--provider-profile", type=Path, default=Path.home() / ".config/anicca/crowdworks/public-profile.json")
    parser.add_argument("--application-receipts-path", type=Path, default=DEFAULT_APPLICATION_RECEIPTS)
    args = parser.parse_args(argv); provider = profile_module.load_config(args.provider_profile)
    return CrowdWorksPaidAdapter(account_id=args.account_id, state_path=args.state_path.expanduser().resolve(), candidate_profile=args.candidate_profile.expanduser().resolve(), provider_profile=provider,
                                 application_receipts_path=args.application_receipts_path), decide


__all__ = ["CrowdWorksPaidAdapter", "CrowdWorksPaidWait", "build", "decide", "read_only_inventory"]
