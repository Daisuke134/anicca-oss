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

PLANNER_SPEC = importlib.util.spec_from_file_location(
    "anicca_shared_reply_planner",
    HERE.parents[2] / "_shared/marketplace-core/scripts/reply_planner.py",
)
if PLANNER_SPEC is None or PLANNER_SPEC.loader is None:
    raise RuntimeError("reply_planner_unavailable")
reply_planner = importlib.util.module_from_spec(PLANNER_SPEC)
sys.modules[PLANNER_SPEC.name] = reply_planner
PLANNER_SPEC.loader.exec_module(reply_planner)

GROUNDING_SPEC = importlib.util.spec_from_file_location(
    "anicca_shared_reply_grounding",
    HERE.parents[2] / "_shared/marketplace-core/scripts/reply_grounding.py",
)
if GROUNDING_SPEC is None or GROUNDING_SPEC.loader is None:
    raise RuntimeError("reply_grounding_unavailable")
reply_grounding = importlib.util.module_from_spec(GROUNDING_SPEC)
sys.modules[GROUNDING_SPEC.name] = reply_grounding
GROUNDING_SPEC.loader.exec_module(reply_grounding)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _message_body(value: Any) -> str:
    """Normalize the provider's CRLF storage without changing message content."""
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


class LancersReplyAdapter:
    def __init__(self, state_path: Path, grounding: Mapping[str, Any] | None = None):
        self.state_path = state_path
        self.browser = None
        self.page = None
        self._lock = None
        self._boards: dict[str, tuple[Mapping[str, Any], Mapping[str, Any], list[Mapping[str, Any]]]] = {}
        self._posted: dict[str, str] = {}
        self._verified_proposals: set[str] = set()
        self._grounding = dict(grounding or {})

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
            sender = row.get("send_user")
            if not isinstance(sender, Mapping) or type(sender.get("is_client")) is not bool:
                raise work_sync.SourceFailure("message_sender_identity_unavailable")
            conversation.append({
                "event_id": work_sync._id(row.get("id")),
                "role": "buyer" if sender["is_client"] else "seller",
                "body": str(row.get("description") or "").strip(),
            })
        proposal = None
        related = detail.get("with")
        if isinstance(related, Mapping):
            candidate = related.get("proposal")
            if isinstance(candidate, Mapping) and candidate.get("id") is not None:
                proposal_id = work_sync._id(candidate.get("id"))
                if proposal_id in self._verified_proposals:
                    proposal = work_sync._proposal_context(
                        self.page, detail, self._verified_proposals
                    )
        return {
            "board": {"title": board.get("title"), "description": board.get("description")},
            "conversation": conversation,
            "reply_required": bool(conversation and conversation[-1]["role"] == "buyer"),
            "verified_proposal": proposal,
            "grounding": self._grounding,
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
                    and _message_body(row.get("description")) == _message_body(body)):
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


def compose(context: dict[str, Any], state_path: Path) -> str | None:
    conversation = context.get("conversation") or []
    board = context["board"]
    messages = [
        {"id": item["event_id"], "description": item["body"],
         "is_required_reply": item["role"] == "buyer"}
        for item in conversation
    ]
    return work_sync._compose_reply(
        board, messages, state_path,
        {**dict(context.get("grounding") or {}),
         "verified_proposal": context.get("verified_proposal")},
    )


def build(argv: list[str]):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-path", required=True, type=Path)
    parser.add_argument(
        "--candidate-profile", type=Path,
        default=Path.home() / ".config/anicca/job-search/profile.json",
    )
    parser.add_argument(
        "--provider-profile", type=Path,
        default=Path.home() / ".config/anicca/crowdworks/public-profile.json",
    )
    args = parser.parse_args(argv)
    state_path = args.state_path.expanduser().resolve()
    grounding = reply_grounding.build_reply_grounding(
        candidate_profile_path=args.candidate_profile,
        provider_profile_path=args.provider_profile,
    )
    adapter = LancersReplyAdapter(state_path, grounding)

    return adapter, reply_planner.ReplyPlanner(
        lambda context: compose(context, state_path)
    )
