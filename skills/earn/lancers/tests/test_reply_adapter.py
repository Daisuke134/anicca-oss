import importlib.util
from pathlib import Path


MODULE = Path(__file__).parents[1] / "scripts" / "reply_adapter.py"
SPEC = importlib.util.spec_from_file_location("lancers_reply_adapter_test", MODULE)
adapter_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(adapter_module)


def test_no_buyer_event_is_a_noop():
    row = {
        "context": {
            "reply_required": False,
            "conversation": [{"role": "seller", "event_id": "1", "body": "sent"}],
        },
        "state_path": "/tmp/state.json",
    }
    assert adapter_module.decide(row) == {
        "action": "noop", "classification": "awaiting_buyer"
    }


def test_buyer_event_uses_existing_natural_language_composer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        adapter_module.work_sync,
        "_compose_reply",
        lambda board, messages, state, grounding: calls.append(
            (board, messages, state, grounding)
        ) or "承知しました。",
    )
    result = adapter_module.decide({
        "context": {
            "reply_required": True,
            "board": {"title": "相談", "description": "詳細"},
            "conversation": [{"role": "buyer", "event_id": "9", "body": "対応できますか"}],
            "verified_proposal": {"proposal_id": "7"},
        },
        "state_path": "/tmp/reply/state.json",
    })
    assert result == {"action": "reply", "payload": {"body": "承知しました。"}}
    assert calls[0][1][0]["is_required_reply"] is True
    assert calls[0][3]["verified_proposal"]["proposal_id"] == "7"


def test_adapter_mutation_is_only_lancers_reply(monkeypatch, tmp_path):
    adapter = adapter_module.LancersReplyAdapter(tmp_path / "state.json")
    class Page:
        def evaluate(self, script, payload):
            assert payload["path"] == "/v1/message_api/boards/12/messages"
            assert payload["body"] == "ok"
            return {"ok": True, "body": {"data": {"id": "55"}}}
    adapter.page = Page()
    intent = {"action": "reply", "thread_id": "12", "effect_key": "key", "payload": {"body": "ok"}}
    adapter.mutate(intent)
    assert adapter._posted == {"key": "55"}


def test_unverified_proposal_does_not_discard_buyer_conversation(monkeypatch, tmp_path):
    adapter = adapter_module.LancersReplyAdapter(tmp_path / "state.json")
    adapter.page = object()
    adapter._boards = {
        "12": (
            {"id": "12", "title": "相談", "description": "詳細", "is_required_reply": True},
            {"id": "12", "with": {"proposal": {"id": "999"}}},
            [{"id": "7", "board_id": "12", "description": "対応できますか", "is_required_reply": True}],
        )
    }
    monkeypatch.setattr(
        adapter_module.work_sync, "_proposal_context",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unverified grounding was read")),
    )
    context = adapter.context("12")
    assert context["verified_proposal"] is None
    assert context["conversation"][-1]["body"] == "対応できますか"
