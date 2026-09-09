#!/usr/bin/env python3
"""Thin Mercor adapter for the shared marketplace Reply kernel."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parseaddr
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlparse


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SHARED = HERE.parents[2] / "_shared/marketplace-core/scripts"
SCHEMA = HERE.parent / "schemas/reply_decision.schema.json"
AGENT_RUNNER = ROOT / "runtime/agent-runner/agent_runner.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


grounding_module = _load("mercor_reply_grounding", SHARED / "reply_grounding.py")
planner_module = _load("mercor_reply_planner", SHARED / "reply_planner.py")


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"mercor_{name}_invalid")
    return value.strip()


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MercorReplyAdapter:
    def __init__(self, *, snapshot: Path, grounding: Mapping[str, Any],
                 gmail_account: str, gog: str, state_root: Path):
        self.snapshot = snapshot.expanduser().resolve()
        self.grounding = dict(grounding)
        self.gmail_account = _required(gmail_account, "gmail_account")
        self.gog = _required(gog, "gmail_executable")
        self.state_root = state_root.expanduser().resolve()
        self.rows: dict[str, dict[str, Any]] = {}
        self.posted: dict[str, str] = {}

    def _source(self) -> Mapping[str, Any]:
        try:
            value = json.loads(self.snapshot.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError("mercor_reply_snapshot_unavailable") from None
        if not isinstance(value, Mapping) or value.get("version") != 1:
            raise RuntimeError("mercor_reply_snapshot_invalid")
        return value

    def observe_threads(self) -> list[dict[str, str]]:
        source = self._source()
        observed_at = _required(source.get("observed_at"), "observed_at")
        source_health = source.get("source_health") or {}
        if not isinstance(source_health, Mapping):
            raise RuntimeError("mercor_reply_snapshot_invalid")
        gmail_health = source_health.get("gmail") or {"status": "fresh"}
        if not isinstance(gmail_health, Mapping):
            raise RuntimeError("mercor_reply_snapshot_invalid")
        gmail_status = gmail_health.get("status")
        if gmail_status not in {"fresh", "stale"}:
            raise RuntimeError("mercor_reply_snapshot_invalid")
        gmail_observed_at = observed_at
        if gmail_status == "stale":
            gmail_observed_at = _required(
                gmail_health.get("observed_at"), "gmail_observed_at"
            )
        candidates = source.get("applications", {}).get("applications", [])
        gmail = source.get("gmail", [])
        assessments = source.get("assessments", [])
        notifications = source.get("notifications", {}).get("notifications", [])
        contracts = source.get("contracts", [])
        interviews = source.get("interviews", {}).get("data", [])
        if not all(isinstance(rows, list) for rows in (
            candidates, gmail, assessments, notifications, contracts, interviews,
        )):
            raise RuntimeError("mercor_reply_snapshot_invalid")
        self.rows = {}
        for raw in candidates:
            if not isinstance(raw, Mapping):
                raise RuntimeError("mercor_reply_application_invalid")
            status = _required(raw.get("status"), "application_status")
            if status not in {"applying-started", "rejected"}:
                continue
            candidate_id = _required(raw.get("candidateId"), "candidate_id")
            self.rows[f"application:{candidate_id}"] = {
                "kind": "application", "raw": dict(raw), "observed_at": observed_at,
            }
        for raw in assessments:
            if not isinstance(raw, Mapping) or raw.get("status") not in {"in-progress", "completed"}:
                continue
            assessment_id = _required(raw.get("assessmentId"), "assessment_id")
            self.rows[f"assessment:{assessment_id}"] = {
                "kind": "assessment", "raw": dict(raw), "observed_at": observed_at,
            }
        for raw in notifications:
            if not isinstance(raw, Mapping):
                raise RuntimeError("mercor_reply_notification_invalid")
            communication_id = _required(raw.get("commId"), "notification_id")
            self.rows[f"notification:{communication_id}"] = {
                "kind": "notification", "raw": dict(raw), "observed_at": observed_at,
            }
        for raw in contracts:
            if not isinstance(raw, Mapping):
                raise RuntimeError("mercor_reply_contract_invalid")
            job_id = _required(raw.get("jobId"), "contract_id")
            self.rows[f"contract:{job_id}"] = {
                "kind": "contract", "raw": dict(raw), "observed_at": observed_at,
            }
        for raw in interviews:
            if not isinstance(raw, Mapping):
                raise RuntimeError("mercor_reply_interview_invalid")
            interview_id = _required(raw.get("interviewId"), "interview_id")
            self.rows[f"interview:{interview_id}"] = {
                "kind": "interview", "raw": dict(raw), "observed_at": observed_at,
            }
        for raw in gmail:
            if not isinstance(raw, Mapping):
                raise RuntimeError("mercor_reply_gmail_invalid")
            thread_id = _required(raw.get("threadId"), "gmail_thread_id")
            messages = raw.get("messages")
            if not isinstance(messages, list) or not messages or not all(
                isinstance(message, Mapping) for message in messages
            ):
                raise RuntimeError("mercor_reply_gmail_invalid")
            ordered = sorted(
                (dict(message) for message in messages),
                key=lambda message: str(message.get("internalDate") or ""),
            )
            self.rows[f"gmail:{thread_id}"] = {
                "kind": "gmail",
                "raw": {"threadId": thread_id, "messages": ordered},
                "observed_at": gmail_observed_at,
            }
        if gmail_status == "stale":
            self.rows["source:gmail"] = {
                "kind": "source_health",
                "raw": {"status": "stale", "observed_at": gmail_observed_at},
                "observed_at": observed_at,
            }
        return [self._observation(thread_id, row) for thread_id, row in self.rows.items()]

    @staticmethod
    def _event(row: Mapping[str, Any]) -> str:
        raw = row["raw"]
        if row["kind"] == "application":
            return _digest({key: raw.get(key) for key in (
                "candidateId", "listingId", "status", "updatedAt", "next_step"
            )})
        if row["kind"] == "assessment":
            return _digest({key: raw.get(key) for key in (
                "assessmentId", "status", "currentAttempt", "feedbackSentAt"
            )})
        if row["kind"] == "notification":
            return _digest({key: raw.get(key) for key in (
                "commId", "commEvent", "createdAt", "notificationOpened",
                "refApplicationId", "refInterviewId", "refListingUid",
            )})
        if row["kind"] == "contract":
            return _digest({key: raw.get(key) for key in (
                "jobId", "status", "updatedAt",
            )})
        if row["kind"] == "interview":
            return _digest({key: raw.get(key) for key in (
                "interviewId", "status", "createdAt", "resetCount",
            )})
        if row["kind"] == "source_health":
            return _digest({key: raw.get(key) for key in ("status", "observed_at")})
        messages = raw.get("messages") or []
        return _required(messages[-1].get("id"), "gmail_message_id")

    def _observation(self, thread_id: str, row: Mapping[str, Any]) -> dict[str, str]:
        result = {"provider": "mercor", "account_id": self.gmail_account,
                  "thread_id": thread_id, "latest_event_id": self._event(row),
                  "observed_at": _required(row.get("observed_at"), "observed_at")}
        if row["kind"] == "source_health":
            result["pending_reason"] = "provider_source_stale"
        return result

    def observe_one(self, thread_id: str) -> dict[str, str]:
        if thread_id not in self.rows:
            self.observe_threads()
        try:
            return self._observation(thread_id, self.rows[thread_id])
        except KeyError:
            raise RuntimeError("mercor_reply_event_unavailable") from None

    def context(self, thread_id: str) -> dict[str, Any]:
        row = self.rows[thread_id]
        raw, kind = row["raw"], row["kind"]
        all_assessments = self._source().get("assessments", [])
        if kind == "application":
            title = _required(raw.get("title"), "application_title")
            url = f"https://work.mercor.com/jobs/apply/{_required(raw.get('candidateId'), 'candidate_id')}?returnPath=%2Fhome"
            body = f"Application state: {raw.get('status')}. Next step: {raw.get('next_step')}."
            required = raw.get("status") == "applying-started"
            return {"event_kind": kind, "title": title, "official_url": url,
                    "conversation": [{"event_id": self._event(row),
                                      "role": "buyer" if required else "seller", "body": body}],
                    "reply_required": False, "decision_required": required,
                    "assessments": all_assessments, "grounding": self.grounding,
                    "provider_rules": {"person_bound_assessment": "human"}}
        if kind == "assessment":
            title = _required(raw.get("title"), "assessment_title")
            url = "https://work.mercor.com/home?tab=assessments"
            return {"event_kind": kind, "title": title, "official_url": url,
                    "conversation": [{"event_id": self._event(row),
                                      "role": "seller",
                                      "body": f"Assessment state: {raw.get('status')}."}],
                    "reply_required": False, "decision_required": False,
                    "grounding": self.grounding,
                    "provider_rules": {
                        "person_bound_assessment": "human",
                        "handoff_owner": "the related application work item",
                    }}
        if kind == "notification":
            title = str(raw.get("commEvent") or "Mercor notification").strip()
            return {"event_kind": kind, "title": title,
                    "official_url": "https://work.mercor.com/home?tab=notifications",
                    "conversation": [{"event_id": self._event(row), "role": "buyer",
                                      "body": str(raw.get("content") or title)}],
                    "reply_required": False, "decision_required": True,
                    "grounding": self.grounding,
                    "provider_rules": {"on_platform_notification": True}}
        if kind == "contract":
            title = str(raw.get("title") or "Mercor contract").strip()
            return {"event_kind": kind, "title": title,
                    "official_url": "https://work.mercor.com/home?tab=contracts",
                    "conversation": [{"event_id": self._event(row), "role": "buyer",
                                      "body": f"Contract state: {raw.get('status')}."}],
                    "reply_required": False, "decision_required": True,
                    "grounding": self.grounding,
                    "provider_rules": {"contract_work_owned_by_paid": True}}
        if kind == "interview":
            title = str(raw.get("title") or raw.get("customInterviewName")
                        or "Mercor interview").strip()
            return {"event_kind": kind, "title": title,
                    "official_url": "https://work.mercor.com/home?tab=assessments",
                    "conversation": [{"event_id": self._event(row), "role": "seller",
                                      "body": f"Interview state: {raw.get('status')}."}],
                    "reply_required": False, "decision_required": False,
                    "grounding": self.grounding,
                    "provider_rules": {
                        "person_bound_interview": "human",
                        "handoff_owner": "the related application work item",
                    }}
        messages = raw["messages"]
        latest = messages[-1]
        sender = _required(latest.get("from"), "gmail_sender")
        labels = latest.get("labels") or []
        if not isinstance(labels, list):
            raise RuntimeError("mercor_reply_gmail_invalid")
        sent = "SENT" in labels
        automated = any(marker in sender.casefold() for marker in (
            "no-reply@mercor.com", "notifications@mercor.com", "auth@mercor.com",
            "team@mercor.com",
        ))
        conversation = []
        for message in messages:
            message_labels = message.get("labels") or []
            if not isinstance(message_labels, list):
                raise RuntimeError("mercor_reply_gmail_invalid")
            body = str(message.get("body") or message.get("subject") or "").strip()
            conversation.append({
                "event_id": _required(message.get("id"), "gmail_message_id"),
                "role": "seller" if "SENT" in message_labels else "buyer",
                "body": body,
            })
        return {"event_kind": kind,
                "title": str(latest.get("subject") or "Mercor message"),
                "official_url": "https://mail.google.com/",
                "gmail_thread_id": raw["threadId"],
                "conversation": conversation,
                "reply_required": not automated and not sent,
                "decision_required": not automated and not sent,
                "grounding": self.grounding,
                "provider_rules": {"automated_sender": automated}}

    def mutate(self, intent: dict[str, Any]) -> None:
        row = self.rows[intent["thread_id"]]
        if row["kind"] != "gmail" or intent.get("action") != "reply":
            raise RuntimeError("mercor_on_platform_reply_unavailable")
        body = _required(intent.get("payload", {}).get("body"), "reply_body")
        message_id = _required(row["raw"]["messages"][-1].get("id"), "gmail_message_id")
        with tempfile.NamedTemporaryFile("w", dir=self.state_root, delete=False,
                                         encoding="utf-8") as handle:
            handle.write(body + "\n")
            body_path = Path(handle.name)
        body_path.chmod(0o600)
        try:
            done = subprocess.run(
                [self.gog, "gmail", "send", "--account", self.gmail_account,
                 "--json", "--no-input", "--reply-to-message-id", message_id,
                 "--reply-all", "--subject",
                 "Re: " + str(row["raw"]["messages"][-1].get("subject") or ""),
                 "--body-file", str(body_path)], capture_output=True, text=True,
                check=False, timeout=60,
            )
        finally:
            body_path.unlink(missing_ok=True)
        if done.returncode != 0:
            raise RuntimeError("mercor_gmail_reply_failed")
        try:
            receipt = json.loads(done.stdout)
            self.posted[intent["effect_key"]] = _required(
                receipt.get("messageId") or receipt.get("id")
                or (receipt.get("message") or {}).get("id"), "gmail_receipt_id"
            )
        except (AttributeError, ValueError):
            raise RuntimeError("mercor_gmail_reply_uncertain") from None

    def readback(self, intent: dict[str, Any]) -> dict[str, Any]:
        row = self.rows.get(intent["thread_id"])
        body = intent.get("payload", {}).get("body")
        if row and row["kind"] == "gmail" and isinstance(body, str):
            thread_id = _required(row["raw"].get("threadId"), "gmail_thread_id")
            try:
                inbound_at = int(row["raw"]["messages"][-1].get("internalDate"))
            except (AttributeError, TypeError, ValueError):
                return {"authoritative_absent": False}
            thread = subprocess.run(
                [self.gog, "gmail", "thread", "get", "--account", self.gmail_account,
                 "--json", "--wrap-untrusted", "--full", "--sanitize-content",
                 thread_id],
                capture_output=True, text=True, check=False, timeout=30,
            )
            if thread.returncode != 0:
                return {"authoritative_absent": False}
            try:
                value = json.loads(thread.stdout)
                thread_value = value.get("thread")
                messages = thread_value.get("messages") if isinstance(thread_value, Mapping) else None
            except (AttributeError, ValueError):
                return {"authoritative_absent": False}
            if not isinstance(messages, list) or not messages:
                return {"authoritative_absent": False}
            for message in messages:
                if not isinstance(message, Mapping):
                    return {"authoritative_absent": False}
                if not all(key in message for key in (
                        "headers", "labelIds", "internalDate", "body")):
                    return {"authoritative_absent": False}
                headers = message["headers"]
                labels = message["labelIds"]
                if (not isinstance(headers, Mapping) or not headers
                        or not isinstance(labels, list) or not labels
                        or not isinstance(message["body"], str)
                        or not message["body"].strip()):
                    return {"authoritative_absent": False}
                try:
                    sent_at = int(message.get("internalDate"))
                except (TypeError, ValueError):
                    return {"authoritative_absent": False}
                sender = parseaddr(str(headers.get("from") or ""))[1].casefold()
                account = parseaddr(self.gmail_account)[1].casefold()
                outgoing = "SENT" in labels and sender == account and sent_at > inbound_at
                if outgoing and message["body"].strip() == body.strip():
                    message_id = _required(message.get("id"), "gmail_message_id")
                    return {"verified": True, "provider_receipt_id": message_id,
                            "observed_at": _now()}
        return {"authoritative_absent": True}

    def classify_mutation_error(self, error: Exception) -> dict[str, Any] | None:
        if str(error) == "mercor_on_platform_reply_unavailable":
            return {"reason": "mercor_official_reply_control_required",
                    "remaining_work": ["Wait for an official reply control for this event"]}
        return None


def _compose(context: Mapping[str, Any], state_root: Path) -> Mapping[str, Any]:
    prompt = """Mercorの公式イベントをseller本人として処理する。判断はCONTEXT全体から行う。
- person-bound interview/assessment/camera/screen-shareはhuman。開始・代行しない。
- 人間操作不要で相手が回答を求めるGmailはreply。自然で簡潔に全質問へ答える。
- 不採用、応募受付、完了済み、automated通知、返答不要はnoop。
- providerの外部状態待ちはwait。
- humanでは必要操作を具体的にremaining_workへ書く。内部実装や推測をbuyerへ送らない。
CONTEXT:\n""" + json.dumps(dict(context), ensure_ascii=False, sort_keys=True)
    state_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mercor-reply-", dir=state_root) as temporary:
        evidence = Path(temporary) / "evidence"
        done = subprocess.run(
            [sys.executable, str(AGENT_RUNNER), "--task-class", "composition-agent",
             "--prompt-stdin", "--schema", str(SCHEMA), "--evidence-dir", str(evidence),
             "--task-label", "mercor-reply", "--loop", "mercor-reply",
             "--workdir", str(ROOT)], input=prompt, text=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90,
            check=False,
        )
        if done.returncode != 0:
            raise RuntimeError("mercor_reply_planner_failed")
        try:
            summary = json.loads((evidence / "summary.json").read_text())
            result_path = Path(summary["result_path"]).resolve()
            result_path.relative_to(evidence.resolve())
            return json.loads(result_path.read_text())
        except (OSError, KeyError, TypeError, ValueError):
            raise RuntimeError("mercor_reply_planner_failed") from None


def _decision(row: dict[str, Any], state_root: Path) -> dict[str, Any]:
    context = row["context"]

    def compose(value):
        result = dict(_compose(value, state_root))
        if result.get("action") == "human":
            result["handoff"] = {
                "title": _required(context.get("title"), "handoff_title"),
                "url": _required(context.get("official_url"), "handoff_url"),
                "deadline": "公式期限表示なし",
            }
        return result

    return planner_module.ReplyPlanner(compose)(row)


def build(argv: list[str]):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--gmail-account", required=True)
    parser.add_argument("--gog", default="gog")
    parser.add_argument("--candidate-profile", type=Path,
                        default=Path.home() / ".config/anicca/job-search/profile.json")
    args = parser.parse_args(argv)
    grounding = grounding_module.build_reply_grounding(
        candidate_profile_path=args.candidate_profile,
        provider_profile_path=Path.home() / ".config/anicca/job-search/profile.json",
    )
    adapter = MercorReplyAdapter(snapshot=args.snapshot, grounding=grounding,
                                 gmail_account=args.gmail_account, gog=args.gog,
                                 state_root=args.state_root)

    return adapter, lambda row: _decision(row, args.state_root)


__all__ = ["MercorReplyAdapter", "build"]
