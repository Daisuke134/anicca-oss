import importlib.util
from pathlib import Path


MODULE = Path(__file__).parents[1] / "scripts" / "coconala_reply_adapter.py"
SPEC = importlib.util.spec_from_file_location("coconala_reply_adapter_test", MODULE)
adapter_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(adapter_module)


def test_provider_rows_are_normalized_without_owning_lifecycle(tmp_path):
    adapter = adapter_module.CoconalaReplyAdapter(
        state_root=tmp_path,
        inventory_reader=lambda: [{
            "talkroom_id": "12",
            "last_message_identity_sha256": "a" * 64,
        }],
        thread_reader=lambda _thread: ({
            "conversation": [{"side": "buyer", "message_id": "m1", "body": "質問"}],
        }, {"last_sender": "buyer"}),
        sender=lambda *_args: (_ for _ in ()).throw(AssertionError("not called")),
    )
    rows = adapter.observe_threads()
    assert rows[0]["provider"] == "coconala"
    assert rows[0]["thread_id"] == "12"
    assert rows[0]["latest_event_id"] == "a" * 64
    refreshed = adapter.observe_one("12")
    assert refreshed["latest_event_id"] == "m1"
    assert adapter.context("12")["conversation"][-1]["body"] == "質問"


def test_buyer_last_uses_model_composer_and_seller_last_is_noop(tmp_path):
    seen = []
    composer = lambda context: seen.append(context) or "承知しました。"
    buyer = {"context": {"conversation": [{"role": "buyer", "body": "対応できますか"}]}}
    seller = {"context": {"conversation": [{"role": "seller", "body": "回答済み"}]}}
    planner = adapter_module.reply_planner.ReplyPlanner(composer)
    assert planner(buyer) == {
        "action": "reply", "payload": {"body": "承知しました。"}
    }
    assert planner(seller) == {
        "action": "noop", "classification": "awaiting_buyer"
    }
    assert len(seen) == 1


def test_mutation_and_official_readback_remain_provider_specific(tmp_path):
    effects = []

    def thread_reader(_thread):
        conversation = [{"side": "buyer", "message_id": "m1", "body": "質問"}]
        if effects:
            conversation.append({"side": "seller", "message_id": "m2", "body": "回答"})
        return {"conversation": conversation}, {"last_sender": conversation[-1]["side"]}

    def sender(thread, body, expected):
        assert (thread, body, expected) == ("12", "回答", "m1")
        effects.append(body)
        return {"provider_receipt_id": "m2", "observed_at": "2026-09-08T00:00:01Z"}

    adapter = adapter_module.CoconalaReplyAdapter(
        state_root=tmp_path,
        inventory_reader=lambda: [], thread_reader=thread_reader, sender=sender,
    )
    intent = {
        "action": "reply", "thread_id": "12", "latest_event_id": "m1",
        "effect_key": "effect", "payload": {"body": "回答"},
    }
    assert adapter.readback(intent) == {"authoritative_absent": True}
    adapter.mutate(intent)
    assert adapter.readback(intent)["provider_receipt_id"] == "m2"
    assert effects == ["回答"]


def test_default_runtime_paths_stay_inside_the_release(tmp_path):
    adapter = adapter_module.CoconalaReplyAdapter(
        state_root=tmp_path,
        inventory_reader=lambda: [],
        thread_reader=lambda _thread: ({}, {}),
        sender=lambda *_args: {},
    )
    assert adapter.cdp_helper == (
        adapter_module.REPO_ROOT / "skills/browser/scripts/cdp_default_tab.py"
    )
    assert adapter.cdp_helper.is_file()


def test_semantic_composer_projects_validated_judgement_without_provider_decide():
    class Adapter:
        def semantic_dom(self, thread_id):
            assert thread_id == "12"
            return {"version": "official"}

        def official_application_context(self, _thread_id):
            raise AssertionError("application context not requested")

    calls = []

    def judge(dom, url, **kwargs):
        calls.append((dom, url, kwargs))
        return {"judgement": {
            "next_action": "send_estimate",
            "required_official_context": "none",
            "estimate_terms": {"title": "開発", "price_jpy": 10000},
        }}

    composer = adapter_module.CoconalaSemanticComposer(Adapter(), judge)
    result = composer({"thread_id": "12"})
    assert result["next_action"] == "send_estimate"
    assert calls[0][1].endswith("/12")


def test_semantic_composer_refreshes_required_official_application_once():
    class Adapter:
        def __init__(self):
            self.version = 1

        def semantic_dom(self, _thread_id):
            return {"version": self.version}

        def official_application_context(self, _thread_id):
            self.version = 2
            return {"proposal_id": "7"}

    calls = []

    def judge(dom, _url, **kwargs):
        calls.append((dom, kwargs))
        if not kwargs:
            return {"judgement": {
                "next_action": "wait",
                "required_official_context": "application",
                "uncertainty": ["公式応募条件"],
            }}
        return {"judgement": {
            "next_action": "reply", "required_official_context": "none",
            "reply_body": "対応可能です。",
        }}

    result = adapter_module.CoconalaSemanticComposer(Adapter(), judge)({"thread_id": "12"})
    assert result["reply_body"] == "対応可能です。"
    assert calls[1] == ({"version": 2}, {"official_context": {"application": {"proposal_id": "7"}}})
