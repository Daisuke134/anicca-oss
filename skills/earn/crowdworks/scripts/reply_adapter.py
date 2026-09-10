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
from urllib.parse import urlencode, urlsplit
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
profile_module = _load("crowdworks_reply_profile", HERE / "profile.py")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise RuntimeError("provider_response_invalid")
    return str(value).strip()


class CrowdWorksReplyAdapter:
    FORM_CONFIRMATION_BODY = (
        "先ほどご案内のGoogleフォームへの回答操作を行いましたが、送信完了画面の確認ができませんでした。"
        "重複回答を避けるため、回答が受領済みかご確認いただけますでしょうか。"
    )
    def __init__(self, grounding: Mapping[str, Any], *, state_path: Path | None = None,
                 candidate_profile: Path | None = None,
                 provider_profile: Mapping[str, Any] | None = None):
        self.grounding = dict(grounding)
        self.state_path = Path(state_path) if state_path is not None else None
        self.candidate_profile = Path(candidate_profile) if candidate_profile is not None else None
        self.provider_profile = dict(provider_profile) if provider_profile is not None else None
        self.browser = None
        self.page = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, list[dict[str, Any]]] = {}

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

    @staticmethod
    def _provider_route(url: str) -> tuple[str, str] | None:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "crowdworks.jp":
            return None
        match = re.fullmatch(r"/(proposals|contracts)/(\d+)", parsed.path)
        return match.groups() if match is not None else None

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
        result = {"provider": "crowdworks", "account_id": "7145638",
                  "thread_id": _text(row.get("thread_id")),
                  "latest_event_id": _text(row.get("id")), "observed_at": _now()}
        if row.get("proposal_status") == "proposed":
            result["decision_version"] = "official-actions-v3"
        return result

    def _open_thread_page(self, thread_id: str) -> None:
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
        if self._provider_route(self.page.url) is None:
            raise RuntimeError("crowdworks_thread_unavailable")

    def _detail(self, thread_id: str) -> list[dict[str, Any]]:
        self._open_thread_page(thread_id)
        if self.page.locator(
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
              const links=[...full?.querySelectorAll('a[href]') || []].map(a=>a.href);
              return {sender, time, body:bodies[0] || '', links};
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
            links = value.get("links")
            if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
                raise RuntimeError("crowdworks_conversation_invalid")
            result.append({"event_id": digest, "role": "seller" if sender == "Kaito｜AI自動化" else "buyer",
                           "sender": sender, "sent_at": sent_at, "body": body,
                           "links": links})
        self.conversations[thread_id] = result
        return result

    def observe_one(self, thread_id: str) -> dict[str, str]:
        row = self.rows.get(thread_id)
        if row is None:
            raise RuntimeError("crowdworks_thread_unavailable")
        self._open_thread_page(thread_id)
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
        required_action = self._contract_action(thread_id) or self._external_form_action(thread_id)
        if required_action is not None:
            result["required_action"] = required_action
        return result

    @staticmethod
    def _google_form_url(url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            return False
        if parsed.netloc == "forms.gle":
            return bool(parsed.path.strip("/"))
        return (parsed.netloc == "docs.google.com"
                and re.fullmatch(r"/forms/d/e/[^/]+/viewform", parsed.path) is not None)

    def _external_form_action(self, thread_id: str) -> dict[str, Any] | None:
        conversation = self.conversations.get(thread_id) or self._detail(thread_id)
        if not conversation:
            return None
        links = sorted({link for row in conversation if row.get("role") == "buyer"
                        for link in row.get("links", []) if self._google_form_url(link)})
        if len(links) != 1:
            return None
        url = links[0]
        return {"action": "external_action", "payload": {
            "kind": "submit_google_form", "url": url,
            "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
            "completion_body": "ご案内いただいたGoogleフォームへの回答を完了しました。ご確認をお願いいたします。",
        }}

    @staticmethod
    def _form_items(raw: Any) -> list[dict[str, Any]]:
        result = []
        if not isinstance(raw, list):
            raise RuntimeError("google_form_metadata_invalid")
        for item in raw:
            if not isinstance(item, list) or len(item) < 5 or not isinstance(item[1], str):
                continue
            entries = []
            if isinstance(item[4], list):
                for entry in item[4]:
                    if not isinstance(entry, list) or not entry or not isinstance(entry[0], int):
                        continue
                    choices = []
                    if len(entry) > 1 and isinstance(entry[1], list):
                        choices = [choice[0] for choice in entry[1]
                                   if isinstance(choice, list) and choice
                                   and isinstance(choice[0], str) and choice[0]]
                    entries.append({"id": entry[0], "choices": choices,
                                    "required": bool(entry[2]) if len(entry) > 2 else False})
            if entries:
                result.append({"title": item[1].strip(), "type": item[3], "entries": entries})
        return result

    def _private_profiles(self) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if self.candidate_profile is None or self.provider_profile is None:
            raise RuntimeError("google_form_profile_unavailable")
        candidate_raw = json.loads(self.candidate_profile.read_text(encoding="utf-8"))
        provider = self.provider_profile
        candidate = candidate_raw.get("candidate") if isinstance(candidate_raw, Mapping) else None
        if not isinstance(candidate, Mapping) or not isinstance(provider, Mapping):
            raise RuntimeError("google_form_profile_invalid")
        return candidate, provider

    def _question_answers(self, items: list[dict[str, Any]]) -> list[tuple[str, str]]:
        candidate, provider = self._private_profiles()
        facts = " ".join(str(row.get("claim") or "") for row in
                         json.loads(self.candidate_profile.read_text(encoding="utf-8")).get("facts", []))
        if "Nara Institute" not in facts or "Mitsubishi UFJ" not in facts:
            raise RuntimeError("google_form_profile_incomplete")
        email = _text(candidate.get("application_email"))
        name_parts = candidate.get("name_romaji_parts")
        if not isinstance(name_parts, Mapping):
            raise RuntimeError("google_form_profile_incomplete")
        initials = "".join(_text(name_parts.get(key))[0].upper() for key in ("family", "given"))
        values: dict[str, str | list[str]] = {
            "①クラウドワークスのユーザー名": _text(provider.get("display_name")),
            "②クラウドワークスのユーザーID": _text(provider.get("provider_employee_id")),
            "③本名のイニシャル": initials,
            "④性別": "男性", "⑤年齢": "20歳～24歳", "⑥お住い": "東京都",
            "⑦最終学歴": "大学院卒", "⑧今まで企業": "はい",
            "⑪メインで使用": "Mac", "⑫仕事でお使い": "光回線",
            "⑬弊社専用アプリ": "はい", "⑭長期稼働": ["ない"],
            "⑮稼働するに辺り": "副業希望",
            "⑯PC使用": ["検索エンジンなどを使い、知らない言葉や必要な情報を自分で調べることができる", "チャットやメールが使える", "ZoomやGoogleMeet等のWeb会議ツールが使える", "SNSへの投稿ができる", "OfficeツールやGoogle系ツールが使える"],
            "⑰PCを使って": "はい", "メインに稼働": "平日・土日祝両方稼働可能",
            "毎月コンスタント": "月100時間以上（週25時間以上）",
            "平日日中にメール": "取れる", "平日日中（ビジネス": "取れる",
            "企業様との連絡手段": ["往訪", "チャットツール（Slack、Chatworkなど）", "メール", "電話"],
            "以下URLでタイピング": "A", "400文字以内で自己PR": "AI・システム開発と業務改善を得意としています。金融機関向けAIエージェントの導入・分析、プロンプト改善、データ分析、Web・アプリ開発の経験があります。連絡と進捗共有を徹底し、必要事項を自ら調査して、正確かつ迅速に仕事を進めます。",
            "職務経歴【1】の職種": "エンジニア・技術職系（システム/ネットワーク/電気/ 電子/ 機械/ 半導体等）",
            "職務経歴【1】の開始": "2025-04-01", "職務経歴【1】の雇用": "正社員",
            "職務経歴【1】の業務内容": "金融機関向けAIエージェントの導入支援、入出力の分析・可視化、プロンプトチューニング、コンテキスト設計。",
            "職務経歴【1】について": "現在も在籍中",
            "ご案内を希望される分野": ["クリエイティブ系（Web/ DTPデザイン、コーディング／エンジニア、動画制作、Webディレクション）"],
        }
        result: list[tuple[str, str]] = []
        for item in items:
            if not item["entries"][0]["required"]:
                continue
            title = item["title"]
            answer = next((value for prefix, value in values.items() if title.startswith(prefix)), None)
            if answer is None and title.startswith("いずれかの職種の実務経験が2年以上"):
                answer = ("いずれかの職種で実務経験が2年以上ある"
                          if "エンジニア" in title else "実務経験がない")
            if answer is None:
                raise RuntimeError(f"google_form_required_answer_missing:{title[:40]}")
            answers = answer if isinstance(answer, list) else [answer]
            choices = item["entries"][0]["choices"]
            if choices and any(value not in choices for value in answers):
                raise RuntimeError("google_form_answer_not_offered")
            entry = item["entries"][0]["id"]
            if item["type"] == 9:
                year, month, day = str(answers[0]).split("-")
                result.extend([(f"entry.{entry}_year", year),
                               (f"entry.{entry}_month", month),
                               (f"entry.{entry}_day", day)])
                continue
            result.extend((f"entry.{entry}", str(value)) for value in answers)
        result.append(("emailAddress", email))
        return result

    def _form_receipt_path(self, url_sha256: str) -> Path:
        if self.state_path is None:
            raise RuntimeError("google_form_state_unavailable")
        return self.state_path.parent / "external-actions" / f"{url_sha256}.json"

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(value), ensure_ascii=False, sort_keys=True),
                             encoding="utf-8")
        temporary.replace(path)

    def _submit_google_form(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        url = _text(payload.get("url"))
        url_sha256 = _text(payload.get("url_sha256"))
        if not self._google_form_url(url) or hashlib.sha256(url.encode()).hexdigest() != url_sha256:
            raise RuntimeError("google_form_intent_invalid")
        receipt_path = self._form_receipt_path(url_sha256)
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if (isinstance(receipt, Mapping)
                    and receipt.get("url_sha256") == url_sha256
                    and receipt.get("confirmation_sha256")):
                return receipt
            if isinstance(receipt, Mapping) and receipt.get("status") == "prepared":
                raise RuntimeError("google_form_submission_uncertain")
            raise RuntimeError("google_form_receipt_invalid")
        form = self.browser.contexts[0].new_page()
        try:
            form.goto(url, wait_until="domcontentloaded", timeout=20_000)
            form.wait_for_timeout(4_000)
            raw = form.evaluate("window.FB_PUBLIC_LOAD_DATA_ && window.FB_PUBLIC_LOAD_DATA_[1][1]")
            answers = self._question_answers(self._form_items(raw))
            action = str(form.locator("form").get_attribute("action") or "")
            parsed = urlsplit(action)
            if (parsed.scheme != "https" or parsed.netloc != "docs.google.com"
                    or re.fullmatch(r"/forms/d/e/[^/]+/formResponse", parsed.path) is None):
                raise RuntimeError("google_form_action_invalid")
            fields = []
            for name in ("fvv", "draftResponse", "pageHistory", "fbzx"):
                locator = form.locator(f'input[name="{name}"]')
                if locator.count() == 1:
                    fields.append((name, str(locator.input_value())))
            fields.extend(answers)
            self._write_json(receipt_path, {
                "version": 1, "status": "prepared", "url_sha256": url_sha256,
                "prepared_at": _now(),
            })
            response = self.browser.contexts[0].request.post(
                action, data=urlencode(fields), headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": form.url,
                }, timeout=30_000,
            )
            body = response.text()
            if response.status != 200 or not any(marker in body for marker in (
                    "回答を記録しました", "Your response has been recorded")):
                raise RuntimeError("google_form_submission_unverified")
            receipt = {"version": 1, "status": "confirmed", "url_sha256": url_sha256,
                       "confirmation_sha256": hashlib.sha256(body.encode()).hexdigest(),
                       "observed_at": _now()}
            self._write_json(receipt_path, receipt)
            return receipt
        finally:
            form.close()

    def _send_reply_once(self, thread_id: str, body: str) -> None:
        rows = self._detail(thread_id)
        if any(row["role"] == "seller" and row["body"].replace("\r\n", "\n")
               == body.replace("\r\n", "\n") for row in rows):
            return
        self.page.locator('textarea[name="message[body]"]').fill(body)
        self.page.get_by_role("button", name="メッセージを投稿する", exact=True).click()
        self.page.wait_for_timeout(2_000)

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

    @staticmethod
    def _same_contract_offer(current: Mapping[str, Any], persisted: Mapping[str, Any]) -> bool:
        core = ("condition_id", "terms_sha256", "title", "amount")
        return all(current.get(field) == persisted.get(field) for field in core)

    def mutate(self, intent: dict[str, Any]) -> None:
        if intent.get("action") == "accept_contract":
            self._open_thread_page(intent["thread_id"])
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
        if intent.get("action") == "external_action":
            payload = intent.get("payload")
            if not isinstance(payload, Mapping) or payload.get("kind") != "submit_google_form":
                raise RuntimeError("crowdworks_external_action_unsupported")
            self._open()
            receipt_path = self._form_receipt_path(_text(payload.get("url_sha256")))
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if isinstance(receipt, Mapping) and receipt.get("status") == "prepared":
                    self._send_reply_once(intent["thread_id"], self.FORM_CONFIRMATION_BODY)
                    self._write_json(receipt_path, {
                        **dict(receipt), "status": "confirmation_requested",
                        "confirmation_thread_id": intent["thread_id"],
                        "confirmation_requested_at": _now(),
                    })
                    return
            self._submit_google_form(payload)
            self._send_reply_once(intent["thread_id"], _text(payload.get("completion_body")))
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
            self._open_thread_page(intent["thread_id"])
            current = self._contract_action(intent["thread_id"])
            persisted = intent.get("payload")
            if (current is not None and isinstance(persisted, Mapping)
                    and self._same_contract_offer(current["payload"], persisted)):
                return {"authoritative_absent": True}
            route = self._provider_route(self.page.url)
            if route is not None and route[0] == "contracts" and isinstance(persisted, Mapping):
                title = str(persisted.get("title") or "")
                amount = str(persisted.get("amount") or "")
                expected = [str(persisted.get(field) or "")
                            for field in ("client", "worker")]
                expected = [value for value in expected if value]
                body = self.page.locator("body").inner_text()
                if (title and amount and title in self.page.title()
                        and amount in body and all(value in body for value in expected)):
                    return {"verified": True,
                            "provider_receipt_id": f"contract:{route[1]}",
                            "observed_at": _now()}
            progress = self.page.locator("div.progress_detail")
            if progress.count() != 1:
                return {}
            current_terms = self.page.locator("table.conditions.recent_condition")
            current_text = current_terms.inner_text() if current_terms.count() == 1 else ""
            title = str(intent.get("payload", {}).get("title") or "")
            amount = str(intent.get("payload", {}).get("amount") or "")
            client = str(intent.get("payload", {}).get("client") or "")
            worker = str(intent.get("payload", {}).get("worker") or "")
            expected = [value for value in (amount, client, worker) if value]
            terms_match = (title and title in self.page.title() and expected
                           and all(value in current_text for value in expected))
            progress_text = progress.inner_text()
            awaiting_client = (
                "まだクライアントが契約に同意していません" in progress_text
                and "クライアントが契約に同意すると契約成立" in progress_text
            )
            if terms_match and awaiting_client:
                condition_id = _text(intent.get("payload", {}).get("condition_id"))
                return {"verified": True,
                        "provider_receipt_id": f"condition-accepted:{condition_id}",
                        "observed_at": _now()}
            links = progress.locator('a[href^="/contracts/"]')
            visible = [links.nth(index) for index in range(links.count())
                       if links.nth(index).is_visible()]
            if len(visible) == 1:
                href = str(visible[0].get_attribute("href") or "")
                match = re.fullmatch(r"/contracts/(\d+)", href)
                if match is not None and terms_match:
                    return {"verified": True,
                            "provider_receipt_id": f"contract:{match.group(1)}",
                            "observed_at": _now()}
            return {}
        if intent.get("action") == "external_action":
            payload = intent.get("payload")
            if not isinstance(payload, Mapping) or payload.get("kind") != "submit_google_form":
                return {"authoritative_absent": True}
            url_sha256 = str(payload.get("url_sha256") or "")
            receipt_path = self._form_receipt_path(url_sha256)
            if not receipt_path.exists():
                return {"authoritative_absent": True}
            try:
                form_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise RuntimeError("google_form_receipt_invalid") from None
            if not isinstance(form_receipt, Mapping) or form_receipt.get("url_sha256") != url_sha256:
                raise RuntimeError("google_form_receipt_invalid")
            if form_receipt.get("status") in {"prepared", "confirmation_requested"}:
                confirmation_thread = form_receipt.get("confirmation_thread_id")
                if (form_receipt.get("status") == "confirmation_requested"
                        and confirmation_thread != intent["thread_id"]):
                    return {}
                rows = self._detail(intent["thread_id"])
                request_index = next((
                    index for index, row in enumerate(rows)
                    if row["role"] == "seller"
                    and row["body"].replace("\r\n", "\n")
                    == self.FORM_CONFIRMATION_BODY.replace("\r\n", "\n")
                ), None)
                if request_index is None:
                    return {"resume_required": True}
                for row in rows[request_index + 1:]:
                    body = row["body"].replace("\r\n", "\n")
                    if row["role"] != "buyer":
                        continue
                    if any(negative in body for negative in (
                            "届いていな", "届いていません", "届いてません", "確認できな", "確認できません",
                            "受領していな", "受領してません", "確認していません", "未確認")):
                        return {}
                    object_bound = "回答" in body or "フォーム" in body
                    if object_bound and any(positive in body for positive in (
                            "確認しました", "確認できました", "受領しました", "届いています",
                            "回答を確認", "回答確認")):
                        return {"verified": True,
                                "provider_receipt_id": "google-form-buyer-confirmed:"
                                + _text(row.get("event_id")), "observed_at": _now()}
                return {}
            body = str(payload.get("completion_body") or "")
            rows = self._detail(intent["thread_id"])
            for row in rows:
                if (row["role"] == "seller" and body
                        and row["body"].replace("\r\n", "\n") == body.replace("\r\n", "\n")):
                    return {"verified": True,
                            "provider_receipt_id": "google-form:" + _text(
                                form_receipt.get("confirmation_sha256")) + ":" + row["event_id"],
                            "observed_at": _now()}
            return {"resume_required": True}
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
    provider_profile = profile_module.load_config(args.provider_profile)
    grounding = grounding_module.build_reply_grounding(
        candidate_profile_path=args.candidate_profile,
        provider_profile=provider_profile,
    )
    adapter = CrowdWorksReplyAdapter(
        grounding, state_path=args.state_path,
        candidate_profile=args.candidate_profile.expanduser().resolve(),
        provider_profile=provider_profile,
    )
    state_root = args.state_path.expanduser().resolve().parent
    return adapter, planner.ReplyPlanner(
        lambda context: composer.compose(context, state_root=state_root,
                                         task_label="crowdworks-reply")
    )
