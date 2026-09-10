#!/usr/bin/env python3
"""Thin CrowdWorks adapter for the shared marketplace Reply kernel."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


HERE = Path(__file__).resolve().parent
SHARED = HERE.parents[2] / "_shared/marketplace-core/scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


account = _load("crowdworks_reply_account", HERE / "account.py")
planner = _load("crowdworks_reply_planner", SHARED / "reply_planner.py")
grounding_module = _load("crowdworks_reply_grounding", SHARED / "reply_grounding.py")
composer = _load("crowdworks_reply_composer", SHARED / "reply_composer.py")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise RuntimeError("provider_response_invalid")
    return str(value).strip()


class CrowdWorksReplyAdapter:
    def __init__(self, grounding: Mapping[str, Any]):
        self.grounding = dict(grounding)
        self.browser = None
        self.page = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, list[dict[str, str]]] = {}

    def _open(self) -> None:
        if self.page is not None:
            return
        if not account._cdp_alive():
            raise RuntimeError("crowdworks_browser_unavailable")
        self.browser = account._browser(account.CDP_URL)
        self.page = self.browser.contexts[0].new_page()
        self.page.set_default_timeout(10_000)

    def _reset_page(self) -> None:
        if self.page is not None:
            try:
                self.page.close()
            except Exception:
                pass
        self.page = self.browser.contexts[0].new_page()
        self.page.set_default_timeout(10_000)

    def _inbox_page_once(self, number: int) -> Mapping[str, Any]:
        self._open()
        with self.page.expect_response(
            lambda response: "/api/v3/message/list?" in response.url,
            timeout=20_000,
        ) as pending:
            self.page.goto(
                f"https://crowdworks.jp/messages/received?page={number}",
                wait_until="domcontentloaded", timeout=20_000,
            )
        response = pending.value
        if response.status != 200:
            raise RuntimeError("crowdworks_inbox_unavailable")
        value = response.json()
        if not isinstance(value, Mapping) or value.get("status") != "success":
            raise RuntimeError("crowdworks_inbox_unavailable")
        data = value.get("data")
        if not isinstance(data, Mapping):
            raise RuntimeError("crowdworks_inbox_unavailable")
        return data

    def _inbox_page(self, number: int) -> Mapping[str, Any]:
        try:
            return self._inbox_page_once(number)
        except PlaywrightTimeoutError:
            self._reset_page()
            return self._inbox_page_once(number)

    def observe_threads(self) -> list[dict[str, str]]:
        self.rows = {}
        page_number = 1
        while True:
            data = self._inbox_page(page_number)
            items = data.get("items")
            pagination = data.get("pagination")
            if not isinstance(items, list) or not all(isinstance(row, Mapping) for row in items):
                raise RuntimeError("crowdworks_inbox_invalid")
            if not isinstance(pagination, Mapping):
                raise RuntimeError("crowdworks_inbox_invalid")
            for raw in items:
                thread_id = _text(raw.get("thread_id"))
                if thread_id in self.rows:
                    raise RuntimeError("crowdworks_inbox_duplicate")
                row = dict(raw)
                row["thread_id"] = thread_id
                row["id"] = _text(raw.get("id"))
                row["sent_at"] = _text(raw.get("sent_at"))
                self.rows[thread_id] = row
            total = int(pagination.get("total_pages", 0))
            if page_number >= total:
                break
            page_number += 1
            if page_number > 100:
                raise RuntimeError("crowdworks_inbox_page_limit")
        return [self._observation(row) for row in self.rows.values()]

    @staticmethod
    def _observation(row: Mapping[str, Any]) -> dict[str, str]:
        return {"provider": "crowdworks", "account_id": "7145638",
                "thread_id": _text(row.get("thread_id")),
                "latest_event_id": _text(row.get("id")), "observed_at": _now(),
                "decision_version": "official-actions-v1"}

    def _detail(self, thread_id: str) -> list[dict[str, str]]:
        row = self.rows.get(thread_id)
        if row is None:
            raise RuntimeError("crowdworks_thread_unavailable")
        url = f"https://crowdworks.jp/messages/{row['id']}"
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeoutError:
            self._reset_page()
            self.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        self.page.wait_for_timeout(1500)
        if "/proposals/" not in self.page.url or self.page.locator(
            'textarea[name="message[body]"]'
        ).count() != 1:
            raise RuntimeError("crowdworks_thread_unavailable")
        values = self.page.locator('div[class*="_messageItem_"]').evaluate_all(
            """nodes => nodes.map(node => {
              const full=[...node.querySelectorAll('div[class*="_messageBody_"]')]
                .find(item => item.querySelector('p'));
              const sender=full?.querySelector('a[class*="_senderName_"]')?.textContent?.trim();
              const time=full?.querySelector('time')?.getAttribute('datetime');
              const bodies=[...full?.querySelectorAll('p') || []]
                .map(item => item.innerText.trim()).filter(Boolean).sort((a,b)=>b.length-a.length);
              return {sender, time, body:bodies[0] || ''};
            })"""
        )
        if not isinstance(values, list) or not values:
            raise RuntimeError("crowdworks_conversation_unavailable")
        result = []
        for value in values:
            if not isinstance(value, Mapping):
                raise RuntimeError("crowdworks_conversation_invalid")
            sender, sent_at, body = (_text(value.get(key)) for key in ("sender", "time", "body"))
            digest = hashlib.sha256(f"{sender}\0{sent_at}\0{body}".encode()).hexdigest()
            result.append({"event_id": digest, "role": "seller" if sender == "Kaito｜AI自動化" else "buyer",
                           "sender": sender, "sent_at": sent_at, "body": body})
        self.conversations[thread_id] = result
        return result

    def observe_one(self, thread_id: str) -> dict[str, str]:
        row = self.rows.get(thread_id)
        if row is None:
            raise RuntimeError("crowdworks_thread_unavailable")
        self._detail(thread_id)
        return self._observation(row)

    def context(self, thread_id: str) -> dict[str, Any]:
        row = self.rows[thread_id]
        conversation = self.conversations.get(thread_id) or self._detail(thread_id)
        result = {"board": {"title": row.get("title"), "proposal_status": row.get("proposal_status")},
                "conversation": conversation[-20:],
                "reply_required": conversation[-1]["role"] == "buyer",
                "grounding": self.grounding,
                "provider_rules": {
                    "outside_contact_before_approval": "forbidden",
                    "auto_accept_official_proposals": True,
                }}
        required_action = self._contract_action(thread_id)
        if required_action is not None:
            result["required_action"] = required_action
        return result

    def _contract_action(self, thread_id: str) -> dict[str, Any] | None:
        row = self.rows.get(thread_id)
        if row is None or row.get("proposal_status") != "proposed":
            return None
        trigger = self.page.locator(
            'a.intro-employer_proposed_project[href="#message-dialog-agreement"]'
        )
        form = self.page.locator(
            'form[action^="/proposal_conditions/"][action$="/agree"]'
        )
        if trigger.count() != 1 or not trigger.is_visible() or form.count() != 1:
            return None
        action = str(form.get_attribute("action") or "")
        match = re.fullmatch(r"/proposal_conditions/(\d+)/agree", action)
        if match is None:
            raise RuntimeError("crowdworks_contract_form_invalid")
        terms = form.locator("table.agreement_condition tr").evaluate_all(
            """rows => Object.fromEntries(rows.map(row => [
              row.querySelector('th')?.innerText.trim() || '',
              row.querySelector('td')?.innerText.trim() || ''
            ]).filter(([key, value]) => key && value))"""
        )
        if not isinstance(terms, Mapping) or not terms:
            raise RuntimeError("crowdworks_contract_terms_invalid")
        canonical = json.dumps(dict(terms), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        return {"action": "accept_contract", "payload": {
            "condition_id": match.group(1),
            "terms_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "title": _text(terms.get("タイトル（仕事名）")),
            "client": _text(terms.get("クライアント（発注者）")),
            "worker": _text(terms.get("ワーカー（受注者）")),
            "amount": _text(terms.get("金額")),
        }}

    def mutate(self, intent: dict[str, Any]) -> None:
        if intent.get("action") == "accept_contract":
            self._detail(intent["thread_id"])
            expected = self._contract_action(intent["thread_id"])
            if expected is None or expected.get("payload") != intent.get("payload"):
                raise RuntimeError("crowdworks_contract_terms_changed")
            self.page.locator(
                'a.intro-employer_proposed_project[href="#message-dialog-agreement"]'
            ).click()
            checkbox = self.page.locator('input[name="check-terms"]')
            submit = self.page.locator('input[value="同意して契約する"]')
            checkbox.check()
            if submit.count() != 1 or not submit.is_visible() or submit.is_disabled():
                raise RuntimeError("crowdworks_contract_submit_unavailable")
            submit.click()
            self.page.wait_for_load_state("domcontentloaded", timeout=20_000)
            return
        if intent.get("action") != "reply":
            raise RuntimeError("crowdworks_estimate_unsupported")
        body = intent.get("payload", {}).get("body")
        if not isinstance(body, str) or not body.strip():
            raise RuntimeError("reply_body_invalid")
        self._detail(intent["thread_id"])
        self.page.locator('textarea[name="message[body]"]').fill(body.strip())
        self.page.get_by_role("button", name="メッセージを投稿する", exact=True).click()
        self.page.wait_for_timeout(2000)

    def readback(self, intent: dict[str, Any]) -> dict[str, Any]:
        if intent.get("action") == "accept_contract":
            self._detail(intent["thread_id"])
            current = self._contract_action(intent["thread_id"])
            if current is not None and current.get("payload") == intent.get("payload"):
                return {"authoritative_absent": True}
            progress = self.page.locator("div.progress_detail")
            if progress.count() != 1:
                return {}
            links = progress.locator('a[href^="/contracts/"]')
            visible = [links.nth(index) for index in range(links.count())
                       if links.nth(index).is_visible()]
            if len(visible) == 1:
                href = str(visible[0].get_attribute("href") or "")
                match = re.fullmatch(r"/contracts/(\d+)", href)
                current_terms = self.page.locator("table.conditions.recent_condition")
                current_text = current_terms.inner_text() if current_terms.count() == 1 else ""
                title = str(intent.get("payload", {}).get("title") or "")
                amount = str(intent.get("payload", {}).get("amount") or "")
                client = str(intent.get("payload", {}).get("client") or "")
                worker = str(intent.get("payload", {}).get("worker") or "")
                expected = [value for value in (amount, client, worker) if value]
                if (match is not None and title and title in self.page.title()
                        and expected and all(value in current_text for value in expected)):
                    return {"verified": True,
                            "provider_receipt_id": f"contract:{match.group(1)}",
                            "observed_at": _now()}
            return {}
        body = intent.get("payload", {}).get("body")
        if not isinstance(body, str):
            return {"authoritative_absent": True}
        rows = self._detail(intent["thread_id"])
        for index, row in enumerate(rows):
            if row["role"] == "seller" and row["body"].replace("\r\n", "\n") == body.replace("\r\n", "\n"):
                receipt_id = row["event_id"]
                inbox = self.rows.get(intent["thread_id"], {})
                if index == len(rows) - 1 and inbox.get("is_replied") is True:
                    receipt_id = _text(inbox.get("id"))
                return {"verified": True, "provider_receipt_id": receipt_id, "observed_at": _now()}
        return {"authoritative_absent": True}

    def close(self) -> None:
        if self.page is not None:
            try:
                self.page.close()
            except Exception:
                pass
        self.page = None
        self.browser = None


def build(argv: list[str]):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-path", required=True, type=Path)
    parser.add_argument("--candidate-profile", type=Path,
                        default=Path.home() / ".config/anicca/job-search/profile.json")
    parser.add_argument("--provider-profile", type=Path,
                        default=Path.home() / ".config/anicca/crowdworks/public-profile.json")
    args = parser.parse_args(argv)
    grounding = grounding_module.build_reply_grounding(
        candidate_profile_path=args.candidate_profile,
        provider_profile_path=args.provider_profile,
    )
    adapter = CrowdWorksReplyAdapter(grounding)
    state_root = args.state_path.expanduser().resolve().parent
    return adapter, planner.ReplyPlanner(
        lambda context: composer.compose(context, state_root=state_root,
                                         task_label="crowdworks-reply")
    )
