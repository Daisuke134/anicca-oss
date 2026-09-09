import importlib.util
import json
from pathlib import Path
import subprocess
import sys


MODULE = Path(__file__).parents[1] / "job_search_loop/mercor_reply_snapshot.py"
SPEC = importlib.util.spec_from_file_location("mercor_reply_snapshot_test", MODULE)
snapshot = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = snapshot
SPEC.loader.exec_module(snapshot)


class FakeWebSocket:
    def __init__(self):
        self.messages = []
        self.bodies = {}

    async def send(self, raw):
        command = json.loads(raw)
        identifier = command["id"]
        method = command["method"]
        if method == "Page.navigate":
            self.messages.append({"id": identifier, "result": {}})
            for index, (name, url) in enumerate(snapshot.ENDPOINTS.items()):
                request_id = f"request-{index}"
                self.bodies[request_id] = {"source": name}
                self.messages.extend([
                    {"method": "Network.responseReceived", "params": {
                        "requestId": request_id,
                        "response": {"url": url, "status": 200},
                    }},
                    {"method": "Network.loadingFinished", "params": {
                        "requestId": request_id,
                    }},
                ])
            return
        if method == "Network.getResponseBody":
            request_id = command["params"]["requestId"]
            self.messages.append({"id": identifier, "result": {
                "body": json.dumps(self.bodies[request_id]),
            }})
            return
        self.messages.append({"id": identifier, "result": {}})

    async def recv(self):
        return json.dumps(self.messages.pop(0))


class Connection:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, *_args):
        return None


def test_capture_reads_each_body_at_loading_finished_without_losing_queued_events(monkeypatch):
    websocket = FakeWebSocket()
    monkeypatch.setattr(
        snapshot.websockets, "connect", lambda *_args, **_kwargs: Connection(websocket)
    )

    result = snapshot.asyncio.run(snapshot._capture("ws://127.0.0.1/devtools/page/1"))

    assert result == {name: {"source": name} for name in snapshot.ENDPOINTS}


def test_gmail_inventory_groups_full_history_by_thread_and_excludes_auth(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
                {"id": "in_1", "threadId": "thread_1",
                 "from": "Recruiter <person@mercor.com>", "subject": "Question"},
                {"id": "auth_out", "threadId": "auth_thread",
                 "from": "owner@example.com", "subject": "Re: Sign in"},
                {"id": "auth_1", "threadId": "auth_thread",
                 "from": "Mercor <auth@mercor.com>", "subject": "Sign in"},
                {"id": "auth_out_2", "threadId": "auth_out_thread",
                 "from": "owner@example.com", "to": "auth@mercor.com",
                 "subject": "Re: Sign in"},
            ]}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"thread": {
            "id": "thread_1", "messages": [
                {"id": "in_1", "threadId": "thread_1", "internalDate": "1",
                 "labelIds": ["INBOX"], "body": "Question",
                 "headers": {"from": "Recruiter <person@mercor.com>",
                             "to": "owner@example.com", "subject": "Question"}},
                {"id": "out_1", "threadId": "thread_1", "internalDate": "2",
                 "labelIds": ["SENT"], "body": "Answer",
                 "headers": {"from": "owner@example.com",
                             "to": "person@mercor.com", "subject": "Re: Question"}},
            ],
        }}), "")

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    result = snapshot._gmail("owner@example.com", "gog")

    assert [row["threadId"] for row in result] == ["thread_1"]
    assert [message["id"] for message in result[0]["messages"]] == ["in_1", "out_1"]
    assert len([argv for argv in calls if "thread" in argv]) == 1


def test_gmail_inventory_reuses_unchanged_full_thread(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        assert "search" in argv
        return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
            {"id": "in_1", "threadId": "thread_1",
             "from": "Recruiter <person@mercor.com>", "subject": "Question"},
        ]}), "")

    prior = [{"threadId": "thread_1", "messages": [
        {"id": "in_1", "threadId": "thread_1", "internalDate": "1",
         "labels": ["INBOX"], "from": "person@mercor.com",
         "to": "owner@example.com", "subject": "Question", "body": "Question"},
        {"id": "out_1", "threadId": "thread_1", "internalDate": "2",
         "labels": ["SENT"], "from": "owner@example.com",
         "to": "person@mercor.com", "subject": "Re: Question", "body": "Answer"},
    ]}]
    monkeypatch.setattr(snapshot.subprocess, "run", run)

    assert snapshot._gmail("owner@example.com", "gog", prior) == prior
    assert len(calls) == 2


def test_gmail_inventory_reuses_legacy_integer_internal_date(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        messages = ([{"id": "in_1", "threadId": "thread_1",
                     "from": "person@mercor.com", "subject": "Question"}]
                    if argv[4].startswith("from:") else [])
        return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": messages}), "")

    prior = [{"threadId": "thread_1", "messages": [
        {"id": "in_1", "threadId": "thread_1", "internalDate": 1788888888000,
         "labels": ["INBOX"], "from": "person@mercor.com",
         "to": "owner@example.com", "subject": "Question", "body": "Question"},
    ]}]
    monkeypatch.setattr(snapshot.subprocess, "run", run)

    assert snapshot._gmail("owner@example.com", "gog", prior) == prior
    assert len(calls) == 2


def test_gmail_inventory_refreshes_thread_with_new_inbound_message(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
                {"id": "in_2", "threadId": "thread_1",
                 "from": "Recruiter <person@mercor.com>", "subject": "Follow-up"},
            ]}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"thread": {
            "messages": [{"id": "in_2", "threadId": "thread_1",
                          "internalDate": "2", "labelIds": ["INBOX"],
                          "body": "Follow-up", "headers": {
                              "from": "Recruiter <person@mercor.com>",
                              "to": "owner@example.com", "subject": "Follow-up"}}],
        }}), "")

    prior = [{"threadId": "thread_1", "messages": [{"id": "in_1"}]}]
    monkeypatch.setattr(snapshot.subprocess, "run", run)

    result = snapshot._gmail("owner@example.com", "gog", prior)
    assert result[0]["messages"][0]["id"] == "in_2"
    assert len([argv for argv in calls if "thread" in argv]) == 1


def test_gmail_inventory_does_not_reuse_cache_without_search_message_id(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
                {"threadId": "thread_1", "from": "person@mercor.com"},
            ]}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"thread": {
            "messages": [{"id": "in_2", "threadId": "thread_1",
                          "internalDate": "2", "labelIds": ["INBOX"], "body": "New",
                          "headers": {"from": "person@mercor.com",
                                      "to": "owner@example.com", "subject": "New"}}],
        }}), "")

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    result = snapshot._gmail(
        "owner@example.com", "gog",
        [{"threadId": "thread_1", "messages": [{"id": "in_1"}]}],
    )
    assert result[0]["messages"][0]["id"] == "in_2"
    assert len([argv for argv in calls if "thread" in argv]) == 1


def test_gmail_inventory_refreshes_matching_id_from_malformed_cache(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "search" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
                {"id": "in_1", "threadId": "thread_1", "from": "person@mercor.com"},
            ]}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"thread": {
            "messages": [{"id": "in_1", "threadId": "thread_1",
                          "internalDate": "1", "labelIds": ["INBOX"], "body": "Question",
                          "headers": {"from": "person@mercor.com",
                                      "to": "owner@example.com", "subject": "Question"}}],
        }}), "")

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    malformed = [{"threadId": "thread_1", "messages": [
        {"id": "in_1", "body": "missing normalized fields"},
    ]}]
    result = snapshot._gmail("owner@example.com", "gog", malformed)
    assert result[0]["messages"][0]["internalDate"] == "1"
    assert len([argv for argv in calls if "thread" in argv]) == 1


def test_gmail_inventory_refreshes_thread_when_new_sent_message_appears(monkeypatch):
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if "search" in argv:
            if argv[4].startswith("in:sent"):
                return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
                    {"id": "out_2", "threadId": "thread_1", "from": "owner@example.com",
                     "subject": "Re: Question"},
                ]}), "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
                {"id": "in_1", "threadId": "thread_1", "from": "person@mercor.com",
                 "to": "owner@example.com", "subject": "Question"},
            ]}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"thread": {
            "messages": [
                {"id": "in_1", "threadId": "thread_1", "internalDate": "1",
                 "labelIds": ["INBOX"], "body": "Question",
                 "headers": {"from": "person@mercor.com", "to": "owner@example.com",
                             "subject": "Question"}},
                {"id": "out_2", "threadId": "thread_1", "internalDate": "2",
                 "labelIds": ["SENT"], "body": "Manual answer",
                 "headers": {"from": "owner@example.com", "to": "person@mercor.com",
                             "subject": "Re: Question"}},
            ],
        }}), "")

    prior = [{"threadId": "thread_1", "messages": [
        {"id": "in_1", "threadId": "thread_1", "internalDate": "1",
         "labels": ["INBOX"], "from": "person@mercor.com", "to": "owner@example.com",
         "subject": "Question", "body": "Question"},
    ]}]
    monkeypatch.setattr(snapshot.subprocess, "run", run)
    result = snapshot._gmail("owner@example.com", "gog", prior)
    assert result[0]["messages"][-1]["labels"] == ["SENT"]
    assert len([argv for argv in calls if "thread" in argv]) == 1


def test_gmail_inventory_splits_fast_inbound_and_sent_searches(monkeypatch):
    queries = []

    def run(argv, **_kwargs):
        if "search" in argv:
            queries.append(argv[4])
            if argv[4].startswith("in:sent"):
                return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
                    {"id": "out_1", "threadId": "thread_1", "from": "owner@example.com",
                     "subject": "Answer"},
                    {"id": "noise", "threadId": "noise", "from": "owner@example.com",
                     "subject": "Mercor notes"},
                ]}), "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": [
                {"id": "in_1", "threadId": "thread_1", "from": "person@mercor.com",
                 "to": "owner@example.com", "subject": "Question"},
            ]}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"thread": {
            "messages": [
                {"id": "in_1", "threadId": "thread_1", "internalDate": "1",
                 "labelIds": ["INBOX"], "body": "Question", "headers": {
                     "from": "person@mercor.com", "to": "owner@example.com",
                     "subject": "Question"}},
                {"id": "out_1", "threadId": "thread_1", "internalDate": "2",
                 "labelIds": ["SENT"], "body": "Answer", "headers": {
                     "from": "owner@example.com", "to": "person@mercor.com",
                     "subject": "Answer"}},
            ],
        }}), "")

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    result = snapshot._gmail("owner@example.com", "gog")

    assert queries == [
        "from:(mercor.com OR mail.mercor.com) newer_than:30d",
        "in:sent mercor newer_than:30d",
    ]
    assert [row["threadId"] for row in result] == ["thread_1"]


def test_gmail_inventory_retries_one_transient_thread_timeout(monkeypatch):
    thread_attempts = 0

    def run(argv, **_kwargs):
        nonlocal thread_attempts
        if "search" in argv:
            messages = ([{"id": "in_1", "threadId": "thread_1",
                          "from": "person@mercor.com", "subject": "Question"}]
                        if argv[4].startswith("from:") else [])
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": messages}), "")
        thread_attempts += 1
        if thread_attempts == 1:
            raise subprocess.TimeoutExpired(argv, 30)
        return subprocess.CompletedProcess(argv, 0, json.dumps({"thread": {
            "messages": [{"id": "in_1", "threadId": "thread_1",
                          "internalDate": "1", "labelIds": ["INBOX"],
                          "body": "Question", "headers": {
                              "from": "person@mercor.com", "to": "owner@example.com",
                              "subject": "Question"}}],
        }}), "")

    monkeypatch.setattr(snapshot.subprocess, "run", run)

    result = snapshot._gmail("owner@example.com", "gog")
    assert result[0]["messages"][0]["id"] == "in_1"
    assert thread_attempts == 2


def test_gmail_inventory_retries_one_transient_search_timeout(monkeypatch):
    inbound_attempts = 0
    sleeps = []

    def run(argv, **_kwargs):
        nonlocal inbound_attempts
        if "search" in argv:
            if argv[4].startswith("from:"):
                inbound_attempts += 1
                if inbound_attempts == 1:
                    raise subprocess.TimeoutExpired(argv, 30)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"messages": []}), "")
        raise AssertionError("empty inventory must not fetch a thread")

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    monkeypatch.setattr(snapshot.time, "sleep", sleeps.append)

    assert snapshot._gmail("owner@example.com", "gog") == []
    assert inbound_attempts == 2
    assert sleeps == [1]


def test_gmail_inventory_reports_secret_free_attempt_outcomes(monkeypatch):
    attempts = 0

    def run(argv, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired(argv, 30)
        return subprocess.CompletedProcess(
            argv, 75, "", "private provider error for owner@example.com"
        )

    monkeypatch.setattr(snapshot.subprocess, "run", run)
    monkeypatch.setattr(snapshot.time, "sleep", lambda _seconds: None)

    try:
        snapshot._gmail("owner@example.com", "gog")
    except RuntimeError as exc:
        assert str(exc) == "mercor_gmail_inventory_unavailable:timeout,exit_75"
        assert "owner@example.com" not in str(exc)
    else:
        raise AssertionError("two failed attempts must fail closed")


def test_snapshot_retries_one_transient_official_source_miss(monkeypatch):
    attempts = 0

    async def capture(_ws_url):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("mercor_reply_sources_missing:applications")
        return {name: {"ok": True} for name in snapshot.ENDPOINTS}

    monkeypatch.setattr(snapshot, "_capture", capture)
    monkeypatch.setattr(snapshot, "_gmail", lambda *_args: [])

    result = snapshot.snapshot(ws_url="ws://127.0.0.1/devtools/page/1",
                               gmail_account="owner@example.com", gog="gog")

    assert attempts == 2
    assert result["applications"] == {"ok": True}


def test_snapshot_keeps_valid_gmail_inventory_on_transient_search_outage(monkeypatch):
    async def capture(_ws_url):
        return {name: {"ok": True} for name in snapshot.ENDPOINTS}

    prior = [{"threadId": "thread_1", "messages": [{
        "id": "in_1", "threadId": "thread_1", "internalDate": "1",
        "labels": ["INBOX"], "from": "person@mercor.com",
        "to": "owner@example.com", "subject": "Question", "body": "Question",
    }]}]
    monkeypatch.setattr(snapshot, "_capture", capture)
    monkeypatch.setattr(
        snapshot, "_gmail",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("mercor_gmail_inventory_unavailable:timeout,timeout")
        ),
    )

    result = snapshot.snapshot(
        ws_url="ws://127.0.0.1/devtools/page/1",
        gmail_account="owner@example.com", gog="gog", previous_gmail=prior,
    )

    assert result["gmail"] == prior
    assert result["source_health"] == {"gmail": {
        "status": "stale",
        "reason": "mercor_gmail_inventory_unavailable:timeout,timeout",
    }}


def test_snapshot_does_not_hide_gmail_outage_without_valid_inventory(monkeypatch):
    async def capture(_ws_url):
        return {name: {"ok": True} for name in snapshot.ENDPOINTS}

    monkeypatch.setattr(snapshot, "_capture", capture)
    monkeypatch.setattr(
        snapshot, "_gmail",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("mercor_gmail_inventory_unavailable:timeout,timeout")
        ),
    )

    for prior in (None, [{"threadId": "thread_1", "messages": [{}]}]):
        try:
            snapshot.snapshot(
                ws_url="ws://127.0.0.1/devtools/page/1",
                gmail_account="owner@example.com", gog="gog", previous_gmail=prior,
            )
        except RuntimeError as exc:
            assert str(exc) == "mercor_gmail_inventory_unavailable:timeout,timeout"
        else:
            raise AssertionError("unproven Gmail inventory must fail closed")
