#!/usr/bin/env python3
"""Thin CrowdWorks adapter for the shared marketplace Reply kernel."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import sys
from typing import Any, Mapping


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

    def _inbox_page(self, number: int) -> Mapping[str, Any]:
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
                "latest_event_id": _text(row.get("id")), "observed_at": _now()}

    def _detail(self, thread_id: str) -> list[dict[str, str]]:
        row = self.rows.get(thread_id)
        if row is None:
            raise RuntimeError("crowdworks_thread_unavailable")
        self.page.goto(f"https://crowdworks.jp/messages/{row['id']}",
                       wait_until="domcontentloaded", timeout=20_000)
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
        return {"board": {"title": row.get("title"), "proposal_status": row.get("proposal_status")},
                "conversation": conversation[-20:],
                "reply_required": conversation[-1]["role"] == "buyer",
                "grounding": self.grounding,
                "provider_rules": {"outside_contact_before_approval": "forbidden"}}

    def mutate(self, intent: dict[str, Any]) -> None:
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
