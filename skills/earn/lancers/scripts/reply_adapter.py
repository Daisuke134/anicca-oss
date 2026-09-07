#!/usr/bin/env python3
"""Thin Lancers adapter for the shared marketplace Reply kernel."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from typing import Any, Mapping
from urllib.parse import quote


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("anicca_lancers_reply_work_sync", HERE / "work_sync.py")
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("work_sync_unavailable")
work_sync = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = work_sync
SPEC.loader.exec_module(work_sync)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LancersReplyAdapter:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.browser = None
        self.page = None
        self._lock = None
        self._boards: dict[str, tuple[Mapping[str, Any], Mapping[str, Any], list[Mapping[str, Any]]]] = {}
        self._posted: dict[str, str] = {}
        self._verified_proposals: set[str] = set()

    def _open(self) -> None:
        if self.page is not None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = work_sync.application_tick.account_lock(
            self.state_path.with_name("reply-account.json")
        )
        self._lock.__enter__()
        try:
            self.browser, self.page = work_sync.application_tick._open_owned_page()
            if not work_sync.application_tick._production_account_ready(self.page):
                raise work_sync.SourceFailure("account_unavailable")
            self._verified_proposals = work_sync._verified_proposals(self.state_path)
        except Exception:
            self.close()
            raise

    def _fetch_messages(self, thread_id: str) -> list[Mapping[str, Any]]:
        self._open()
        return work_sync._message_rows(
            lambda route: work_sync._fetch(self.page, route), thread_id
        )

    def _row(self, thread_id: str, board: Mapping[str, Any], messages: list[Mapping[str, Any]]) -> dict[str, str]:
        latest = max(messages, key=lambda item: int(work_sync._id(item.get("id")))) if messages else None
        latest_event = work_sync._id(latest.get("id")) if latest else work_sync._id(board.get("modified"))
        return {
            "provider": "lancers",
            "account_id": "default",
            "thread_id": thread_id,
            "latest_event_id": latest_event,
            "observed_at": _now(),
        }

    def observe_threads(self) -> list[dict[str, str]]:
        self._open()
        private: list[Any] = []
        work_sync._snapshot(
            lambda route: work_sync._fetch(self.page, route),
            self._verified_proposals,
            private,
        )
        self._boards = {
            work_sync._id(board.get("id")): (board, detail, list(messages))
            for board, detail, messages in private
        }
        return [self._row(thread_id, board, messages)
                for thread_id, (board, _detail, messages) in self._boards.items()]

    def observe_one(self, thread_id: str) -> dict[str, str]:
        if thread_id not in self._boards:
            raise work_sync.SourceFailure("reply_thread_unavailable")
        board, detail, _messages = self._boards[thread_id]
        messages = self._fetch_messages(thread_id)
        self._boards[thread_id] = (board, detail, messages)
        return self._row(thread_id, board, messages)

    def context(self, thread_id: str) -> dict[str, Any]:
        board, detail, messages = self._boards[thread_id]
        conversation = []
        for row in sorted(messages, key=lambda item: int(work_sync._id(item.get("id"))))[-20:]:
            conversation.append({
                "event_id": work_sync._id(row.get("id")),
                "role": "buyer" if row.get("is_required_reply") is True else "seller",
                "body": str(row.get("description") or "").strip(),
            })
        return {
            "board": {"title": board.get("title"), "description": board.get("description")},
            "conversation": conversation,
            "reply_required": bool(board.get("is_required_reply")),
            "verified_proposal": work_sync._proposal_context(
                self.page, detail, self._verified_proposals
            ),
        }

    def mutate(self, intent: dict[str, Any]) -> None:
        if intent["action"] != "reply":
            raise work_sync.SourceFailure("lancers_estimate_unsupported")
        body = intent["payload"].get("body")
        if not isinstance(body, str) or not body.strip():
            raise work_sync.SourceFailure("reply_body_invalid")
        value = self.page.evaluate(
            """async ({path, body}) => { const form = new FormData(); form.append("description", body); form.append("rich_description", body); const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 20000); try { const response = await fetch(path, {method:"POST", credentials:"same-origin", body:form, signal:controller.signal}); const text = await response.text(); if (!response.ok || text.length > 1048576) return {ok:false}; let parsed={}; try { parsed=JSON.parse(text); } catch (_) {} return {ok:true, body:parsed}; } catch (_) { return {ok:false}; } finally { clearTimeout(timer); } }""",
            {"path": f"/v1/message_api/boards/{quote(intent['thread_id'], safe='')}/messages",
             "body": body.strip()},
        )
        if not isinstance(value, Mapping) or value.get("ok") is not True:
            raise work_sync.SourceFailure("reply_submission_uncertain")
        response = value.get("body")
        if isinstance(response, Mapping) and isinstance(response.get("data"), Mapping):
            response = response["data"]
        if not isinstance(response, Mapping):
            raise work_sync.SourceFailure("reply_submission_uncertain")
        self._posted[intent["effect_key"]] = work_sync._id(response.get("id"))

    def readback(self, intent: dict[str, Any]) -> dict[str, Any]:
        body = intent["payload"].get("body")
        if not isinstance(body, str):
            return {"authoritative_absent": True}
        rows = self._fetch_messages(intent["thread_id"])
        provider_id = self._posted.get(intent["effect_key"])
        found = None
        for row in rows:
            if (work_sync._id(row.get("board_id")) == intent["thread_id"]
                    and row.get("description") == body):
                message_id = work_sync._id(row.get("id"))
                if provider_id is None or provider_id == message_id:
                    found = message_id
                    break
        if found is None:
            return {"authoritative_absent": True}
        return {"verified": True, "provider_receipt_id": found, "observed_at": _now()}

    def close(self) -> None:
        if self.page is not None:
            work_sync._cleanup(self.page, self.browser)
        self.page = self.browser = None
        if self._lock is not None:
            lock, self._lock = self._lock, None
            lock.__exit__(None, None, None)


def decide(row: dict[str, Any]) -> dict[str, Any]:
    context = row["context"]
    conversation = context.get("conversation") or []
    if not context.get("reply_required") or not conversation or conversation[-1]["role"] != "buyer":
        return {"action": "noop", "classification": "awaiting_buyer"}
    board = context["board"]
    messages = [
        {"id": item["event_id"], "description": item["body"],
         "is_required_reply": item["role"] == "buyer"}
        for item in conversation
    ]
    body = work_sync._compose_reply(
        board, messages, Path(row["state_path"]),
        {"verified_proposal": context.get("verified_proposal")},
    )
    if body is None:
        return {"action": "noop", "classification": "no_reply"}
    return {"action": "reply", "payload": {"body": body}}


def build(argv: list[str]):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-path", required=True, type=Path)
    args = parser.parse_args(argv)
    adapter = LancersReplyAdapter(args.state_path.expanduser().resolve())

    def decide_with_state(row: dict[str, Any]) -> dict[str, Any]:
        return decide({**row, "state_path": str(args.state_path)})

    return adapter, decide_with_state
