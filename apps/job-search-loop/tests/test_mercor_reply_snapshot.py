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
                {"id": "auth_1", "threadId": "auth_thread",
                 "from": "Mercor <auth@mercor.com>", "subject": "Sign in"},
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
