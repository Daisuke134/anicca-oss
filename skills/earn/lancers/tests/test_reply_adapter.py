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
    planner = adapter_module.reply_planner.ReplyPlanner(
        lambda _context: (_ for _ in ()).throw(AssertionError("model called"))
    )
    assert planner(row) == {
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
    planner = adapter_module.reply_planner.ReplyPlanner(
        lambda context: adapter_module.compose(context, Path("/tmp/reply/state.json"))
    )
    result = planner({
        "context": {
            "reply_required": True,
            "board": {"title": "相談", "description": "詳細"},
            "conversation": [{"role": "buyer", "event_id": "9", "body": "対応できますか"}],
            "verified_proposal": {"proposal_id": "7"},
        },
    })
    assert result == {"action": "reply", "payload": {"body": "承知しました。"}}
    assert calls[0][1][0]["is_required_reply"] is True
    assert calls[0][3]["verified_proposal"]["proposal_id"] == "7"


def test_semantic_uncertainty_becomes_durable_human_wait(monkeypatch):
    def uncertain(*_args, **_kwargs):
        raise adapter_module.work_sync.ReplySemanticUncertain([
            "9月8日から13日までの日別稼働時間",
            "本人がAIを使わず作業するという確約",
        ])

    monkeypatch.setattr(adapter_module.work_sync, "_compose_reply", uncertain)
    planner = adapter_module.reply_planner.ReplyPlanner(
        lambda context: adapter_module.compose(context, Path("/tmp/reply/state.json"))
    )
    result = planner({
        "context": {
            "reply_required": True,
            "board": {"title": "選考", "description": "詳細"},
            "conversation": [{"role": "buyer", "event_id": "9", "body": "回答してください"}],
            "verified_proposal": None,
        },
    })
    assert result == {
        "action": "human",
        "reason": "reply_facts_required",
        "remaining_work": [
            "9月8日から13日までの日別稼働時間",
            "本人がAIを使わず作業するという確約",
        ],
    }


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
            [{"id": "7", "board_id": "12", "description": "対応できますか",
              "is_required_reply": False, "send_user": {"is_client": True}}],
        )
    }
    monkeypatch.setattr(
        adapter_module.work_sync, "_proposal_context",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unverified grounding was read")),
    )
    context = adapter.context("12")
    assert context["verified_proposal"] is None
    assert context["conversation"][-1]["body"] == "対応できますか"
    assert context["conversation"][-1]["role"] == "buyer"
    assert context["reply_required"] is True


def test_sender_identity_not_required_reply_flag_owns_role(monkeypatch, tmp_path):
    adapter = adapter_module.LancersReplyAdapter(tmp_path / "state.json")
    adapter.page = object()
    adapter._boards = {
        "12": (
            {"id": "12", "title": "相談", "description": "詳細", "is_required_reply": False},
            {"id": "12"},
            [
                {"id": "7", "board_id": "12", "description": "よろしいですか？",
                 "is_required_reply": False, "send_user": {"id": 10, "is_client": True}},
            ],
        )
    }
    context = adapter.context("12")
    assert context["conversation"] == [{
        "event_id": "7", "role": "buyer", "body": "よろしいですか？",
    }]
    assert context["reply_required"] is True


def test_context_carries_shared_candidate_grounding(monkeypatch, tmp_path):
    adapter = adapter_module.LancersReplyAdapter(
        tmp_path / "state.json",
        {"candidate": {"age_band": "20代"}, "verified_facts": []},
    )
    adapter._boards["board-1"] = (
        {"id": "board-1", "title": "work"},
        {},
        [{
            "id": "1", "description": "年代を教えてください",
            "send_user": {"is_client": True},
        }],
    )

    context = adapter.context("board-1")

    assert context["grounding"]["candidate"]["age_band"] == "20代"


def test_readback_accepts_provider_crlf_normalization(monkeypatch, tmp_path):
    adapter = adapter_module.LancersReplyAdapter(tmp_path / "state.json")
    adapter._fetch_messages = lambda _thread_id: [{
        "id": "59145491",
        "board_id": "9058411",
        "description": "1．男性・20代です。\r\n2．週31〜40時間です。",
    }]
    result = adapter.readback({
        "thread_id": "9058411",
        "effect_key": "effect-1",
        "payload": {"body": "1．男性・20代です。\n2．週31〜40時間です。"},
    })
    assert result["verified"] is True
    assert result["provider_receipt_id"] == "59145491"


def test_gog_resolution_includes_homebrew_for_launchd(monkeypatch):
    observed = {}

    def which(name, *, path):
        observed.update(name=name, path=path)
        return "/opt/homebrew/bin/gog"

    monkeypatch.setattr(adapter_module.shutil, "which", which)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert adapter_module._gog_bin() == "/opt/homebrew/bin/gog"
    assert observed["name"] == "gog"
    assert "/opt/homebrew/bin" in observed["path"].split(":")


def test_private_calendar_credential_is_in_child_env_not_argv(monkeypatch, tmp_path):
    private_env = tmp_path / ".env"
    private_env.write_text("GOG_KEYRING_PASSWORD='private value'\n", encoding="utf-8")
    private_env.chmod(0o600)
    monkeypatch.delenv("GOG_KEYRING_PASSWORD", raising=False)
    monkeypatch.setenv("LIFE_MANAGER_PRIVATE_ENV", str(private_env))
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, env=kwargs["env"])
        return type("Completed", (), {"returncode": 0, "stdout": '{"events": []}'})()

    monkeypatch.setattr(adapter_module, "_gog_bin", lambda: "/opt/homebrew/bin/gog")
    monkeypatch.setattr(adapter_module.subprocess, "run", run)
    adapter = adapter_module.LancersReplyAdapter(tmp_path / "state.json")
    monkeypatch.setattr(adapter, "_candidate", lambda: {"application_email": "owner@example.test"})

    assert adapter._calendar_events(
        adapter_module.datetime(2026, 9, 10, tzinfo=adapter_module.timezone.utc),
        adapter_module.datetime(2026, 9, 11, tzinfo=adapter_module.timezone.utc),
    ) == []
    assert "private value" not in observed["command"]
    assert observed["env"]["GOG_KEYRING_PASSWORD"] == "private value"


def test_booking_link_becomes_shared_external_action_even_when_seller_is_last(monkeypatch, tmp_path):
    adapter = adapter_module.LancersReplyAdapter(tmp_path / "state.json")
    adapter.page = object()
    adapter._boards = {
        "9064025": (
            {"id": "9064025", "title": "pyrite"}, {},
            [
                {"id": "1", "description": "日程調整をお願いします。\nhttps://yoyaku.triplek-rh.workers.dev/?lid=keiodaisuke",
                 "send_user": {"is_client": True}},
                {"id": "2", "description": "予約いたします。",
                 "send_user": {"is_client": False}},
            ],
        )
    }
    monkeypatch.setattr(adapter, "_choose_booking_slot", lambda _url: {
        "start": "2026-09-11T10:00:00+09:00", "end": "2026-09-11T10:30:00+09:00",
    })

    context = adapter.context("9064025")

    assert context["decision_required"] is True
    assert context["required_action"]["action"] == "external_action"
    assert context["required_action"]["payload"]["kind"] == "schedule_meeting"


def test_external_action_resumes_without_rebooking(monkeypatch, tmp_path):
    adapter = adapter_module.LancersReplyAdapter(tmp_path / "state.json")
    slot = {"start": "2026-09-11T10:00:00+09:00", "end": "2026-09-11T10:30:00+09:00"}
    replies = []
    bookings = [{"slot_start": slot["start"], "slot_end": slot["end"]}]
    monkeypatch.setattr(adapter, "_booking_snapshot", lambda _url: (bookings, []))
    monkeypatch.setattr(adapter, "_calendar_contains", lambda _slot: True)
    monkeypatch.setattr(adapter, "_book", lambda *_args: (_ for _ in ()).throw(AssertionError("rebooked")))
    monkeypatch.setattr(adapter, "_reply_exists", lambda _thread, _body: replies[-1] if replies else None)
    monkeypatch.setattr(adapter, "_post_reply", lambda _thread, _key, _body: replies.append("message-1"))
    intent = {"action": "external_action", "thread_id": "9064025", "effect_key": "key",
              "payload": {"url": "https://yoyaku.triplek-rh.workers.dev/?lid=keiodaisuke",
                          "slot": slot, "completion_body": "予約しました。"}}

    assert adapter.readback(intent) == {"resume_required": True}
    adapter.mutate(intent)
    result = adapter.readback(intent)

    assert result["verified"] is True
    assert replies == ["message-1"]
