import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


MODULE = Path(__file__).parents[1] / "scripts/reply_adapter.py"
SPEC = importlib.util.spec_from_file_location("mercor_reply_adapter_test", MODULE)
reply = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = reply
SPEC.loader.exec_module(reply)


def _snapshot(path: Path):
    path.write_text(json.dumps({
        "version": 1,
        "observed_at": "2026-09-08T09:00:00Z",
        "applications": {"applications": [
            {"candidateId": "candidate_1", "listingId": "list_1",
             "title": "Japanese Writer", "status": "applying-started",
             "updatedAt": "2026-09-08T08:00:00Z", "next_step": "2 of 4 steps"},
            {"candidateId": "candidate_2", "listingId": "list_2",
             "title": "Rejected", "status": "rejected",
             "updatedAt": "2026-09-08T07:00:00Z", "next_step": "Ready"},
            {"candidateId": "candidate_3", "listingId": "list_3",
             "title": "Submitted", "status": "applied",
             "updatedAt": "2026-09-08T06:00:00Z", "next_step": "Ready"},
        ]},
        "assessments": [
            {"assessmentId": "assessment_1", "title": "Bilingual Competency",
             "status": "in-progress", "type": "interview"},
            {"assessmentId": "assessment_2", "title": "Completed",
             "status": "completed", "type": "interview"},
            {"assessmentId": "assessment_3", "title": "Catalog only",
             "status": "not-started", "type": "interview"},
        ],
        "notifications": {"notifications": [
            {"commId": "comm_1", "commEvent": "APPLICATION/ADVANCED",
             "createdAt": "2026-09-08T08:30:00Z", "content": "Next steps",
             "refApplicationId": "candidate_1", "refListingUid": "list_1"},
        ]},
        "contracts": [
            {"jobId": "job_1", "title": "Japanese Writer Contract",
             "status": "active", "updatedAt": "2026-09-08T08:45:00Z"},
        ],
        "interviews": {"status": "success", "data": [
            {"interviewId": "interview_1", "title": "Domain Expert Interview",
             "status": "videoRecordingUploaded", "createdAt": "2026-09-08T08:15:00Z"},
        ]},
        "gmail": [
            {"threadId": "thread_1", "messages": [
                {"id": "mail_1", "threadId": "thread_1", "internalDate": "1",
                 "from": "Recruiter <person@mercor.com>", "subject": "Question",
                 "body": "Can you start Monday?", "labels": ["INBOX"]},
            ]},
            {"threadId": "thread_done", "messages": [
                {"id": "mail_old", "threadId": "thread_done", "internalDate": "1",
                 "from": "Recruiter <person@mercor.com>", "subject": "Old question",
                 "body": "Can you start?", "labels": ["INBOX"]},
                {"id": "mail_sent", "threadId": "thread_done", "internalDate": "2",
                 "from": "owner@example.com", "subject": "Re: Old question",
                 "body": "Yes.", "labels": ["SENT"]},
            ]},
            {"threadId": "receipt_thread", "messages": [
                {"id": "receipt_1", "threadId": "receipt_thread", "internalDate": "1",
                 "from": "Mercor <notifications@mercor.com>",
                 "subject": "Application Submitted - Japanese Writer", "body": "",
                 "labels": ["INBOX"]},
            ]},
        ],
    }), encoding="utf-8")


def _adapter(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    _snapshot(snapshot)
    return reply.MercorReplyAdapter(
        snapshot=snapshot, grounding={}, gmail_account="owner@example.com",
        gog="gog", state_root=tmp_path,
    )


def test_inventory_keeps_actionable_official_events_and_stable_ids(tmp_path):
    adapter = _adapter(tmp_path)
    rows = adapter.observe_threads()
    assert {row["thread_id"] for row in rows} == {
        "application:candidate_1", "application:candidate_2",
        "assessment:assessment_1", "assessment:assessment_2", "gmail:thread_1",
        "gmail:thread_done", "gmail:receipt_thread",
        "notification:comm_1", "contract:job_1", "interview:interview_1",
    }
    assert len({row["latest_event_id"] for row in rows}) == len(rows)


def test_every_official_reply_source_has_context_without_fabricating_actionability(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.observe_threads()

    notification = adapter.context("notification:comm_1")
    contract = adapter.context("contract:job_1")
    interview = adapter.context("interview:interview_1")

    assert notification["event_kind"] == "notification"
    assert contract["event_kind"] == "contract"
    assert interview["event_kind"] == "interview"
    assert interview["decision_required"] is False
    assert all(context["conversation"] for context in (notification, contract, interview))


def test_application_context_keeps_exact_work_item_url(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.observe_threads()
    context = adapter.context("application:candidate_1")
    assert context["decision_required"] is True
    assert context["official_url"].startswith(
        "https://work.mercor.com/jobs/apply/candidate_1"
    )
    assert context["assessments"][0]["title"] == "Bilingual Competency"
    assessment = adapter.context("assessment:assessment_1")
    assert assessment["decision_required"] is False
    assert assessment["provider_rules"]["handoff_owner"] == "the related application work item"


def test_gmail_thread_history_prevents_replying_again_after_our_sent_message(tmp_path):
    adapter = _adapter(tmp_path)
    adapter.observe_threads()
    context = adapter.context("gmail:thread_done")
    assert [message["role"] for message in context["conversation"]] == ["buyer", "seller"]
    assert context["reply_required"] is False
    assert context["decision_required"] is False


def test_model_human_decision_gets_exact_deduplicated_handoff(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    row = adapter.observe_threads()[0]
    row["context"] = adapter.context(row["thread_id"])
    monkeypatch.setattr(reply, "_compose", lambda _context, _root: {
        "action": "human", "reply_body": None, "classification": None,
        "reason": "person_bound_interview",
        "remaining_work": ["Complete the official interview"],
    })
    decision = reply._decision(row, tmp_path)
    assert decision["action"] == "human"
    assert decision["handoff"]["title"] == "Japanese Writer"
    assert decision["handoff"]["url"].startswith("https://work.mercor.com/")


def test_send_receipt_is_not_official_readback(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.observe_threads()
    intent = {
        "thread_id": "gmail:thread_1", "effect_key": "effect-1", "action": "reply",
        "payload": {"body": "Monday works for me."},
    }
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "send" in argv:
            return subprocess.CompletedProcess(argv, 0, '{"messageId":"sent_1"}', "")
        if "thread" in argv:
            return subprocess.CompletedProcess(
                argv, 0, '{"thread":{"messages":[]}}', ""
            )
        raise AssertionError(argv)

    monkeypatch.setattr(reply.subprocess, "run", run)
    adapter.mutate(intent)
    send_argv = next(argv for argv in calls if "send" in argv)
    assert send_argv[send_argv.index("--subject") + 1] == "Re: Question"
    assert adapter.readback(intent) == {"authoritative_absent": False}


def test_official_readback_requires_sent_label_and_our_account(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.observe_threads()
    intent = {
        "thread_id": "gmail:thread_1", "effect_key": "effect-1", "action": "reply",
        "payload": {"body": "Monday works for me."},
    }
    messages = [
        {"id": "historical_ours", "labelIds": ["SENT"], "internalDate": "0",
         "body": "Monday works for me.",
         "headers": {"from": "Owner <owner@example.com>"}},
        {"id": "same_inbound", "labelIds": ["INBOX"], "internalDate": "1",
         "body": "Monday works for me.",
         "headers": {"from": "Recruiter <person@mercor.com>"}},
        {"id": "wrong_sender", "labelIds": ["SENT"], "internalDate": "2",
         "body": "Monday works for me.",
         "headers": {"from": "owner@example.com.attacker.test"}},
    ]

    monkeypatch.setattr(reply.subprocess, "run", lambda argv, **_kwargs:
                        subprocess.CompletedProcess(
                            argv, 0, json.dumps({"thread": {"messages": messages}}), ""
                        ))
    assert adapter.readback(intent) == {"authoritative_absent": True}

    messages.append({"id": "ours", "labelIds": ["SENT"], "internalDate": "3",
                     "body": "Monday works for me.",
                     "headers": {"from": "Owner <owner@example.com>"}})
    result = adapter.readback(intent)
    assert result["verified"] is True
    assert result["provider_receipt_id"] == "ours"


@pytest.mark.parametrize("missing", ["headers", "labelIds", "body"])
def test_official_readback_retries_incomplete_gmail_messages(
        tmp_path, monkeypatch, missing):
    adapter = _adapter(tmp_path)
    adapter.observe_threads()
    intent = {
        "thread_id": "gmail:thread_1", "effect_key": "effect-1", "action": "reply",
        "payload": {"body": "Monday works for me."},
    }
    message = {
        "id": "ours", "labelIds": ["SENT"], "internalDate": "3",
        "body": "Monday works for me.",
        "headers": {"from": "Owner <owner@example.com>"},
    }
    del message[missing]
    monkeypatch.setattr(reply.subprocess, "run", lambda argv, **_kwargs:
                        subprocess.CompletedProcess(
                            argv, 0,
                            json.dumps({"thread": {"messages": [message]}}), ""
                        ))
    assert adapter.readback(intent) == {"authoritative_absent": False}


def test_official_readback_retries_empty_gmail_body(tmp_path, monkeypatch):
    adapter = _adapter(tmp_path)
    adapter.observe_threads()
    intent = {
        "thread_id": "gmail:thread_1", "effect_key": "effect-1", "action": "reply",
        "payload": {"body": "Monday works for me."},
    }
    message = {
        "id": "ours", "labelIds": ["SENT"], "internalDate": "3", "body": "",
        "headers": {"from": "Owner <owner@example.com>"},
    }
    monkeypatch.setattr(reply.subprocess, "run", lambda argv, **_kwargs:
                        subprocess.CompletedProcess(
                            argv, 0,
                            json.dumps({"thread": {"messages": [message]}}), ""
                        ))
    assert adapter.readback(intent) == {"authoritative_absent": False}


def test_owner_and_registry_use_shared_reply_kernel():
    owner = (Path(__file__).parents[1] / "scripts/reply-owner").read_text()
    registry = json.loads((Path(__file__).parents[4] / "config/loop-registry.json").read_text())
    assert "_shared/marketplace-core/scripts/reply_kernel.py" in owner
    assert "mercor_auth_readback" in owner
    assert "mercor_email_auth" not in owner
    row = registry["loops"]["mercor-revenue-reply"]
    assert row["entrypoint"] == "skills/earn/mercor/scripts/reply-owner"
    assert row["cadence"]["start_interval_seconds"] == 300
