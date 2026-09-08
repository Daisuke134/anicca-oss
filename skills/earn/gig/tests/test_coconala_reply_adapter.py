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


def test_coconala_build_passes_shared_grounding_to_semantic_judge(monkeypatch, tmp_path):
    candidate = tmp_path / "candidate.json"
    candidate.write_text('{"candidate":{"gender":"male"},"facts":[]}', encoding="utf-8")
    public = tmp_path / "public.json"
    public.write_text('{"hours_limit":"31-40"}', encoding="utf-8")
    captured = {}

    class Judge:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(adapter_module.requested_estimate, "SemanticJudge", Judge)
    monkeypatch.setattr(
        adapter_module.requested_estimate, "RequestedEstimateComposer",
        lambda **_kwargs: object(),
    )
    adapter_module.build([
        "--state-root", str(tmp_path / "state"),
        "--cdp-helper", str(Path(__file__)),
        "--runner", str(Path(__file__)),
        "--schema", str(Path(__file__)),
        "--estimate-schema", str(Path(__file__)),
        "--candidate-profile", str(candidate),
        "--provider-profile", str(public),
    ])

    assert {row["claim"] for row in captured["seller_facts"]} >= {
        "性別: male", "週あたりの稼働時間: 31-40時間",
    }


def test_semantic_composer_projects_validated_judgement_without_provider_decide():
    class Adapter:
        def semantic_dom(self, thread_id):
            assert thread_id == "12"
            return {"version": "official", "estimate_url": "/direct_offers/add/12"}

        def official_application_context(self, _thread_id):
            raise AssertionError("application context not requested")

    calls = []

    def judge(dom, url, **kwargs):
        calls.append((dom, url, kwargs))
        return {"context_sha256": "a" * 64, "judgement": {
            "next_action": "send_estimate",
            "required_official_context": "none",
            "evidence_message_ids": ["m1"],
            "estimate_terms": {
                "title": "開発", "content": "実装", "quantity": 1,
                "price_jpy": 10000, "delivery_days": 7, "purchase_plan": "single",
            },
        }}

    composer = adapter_module.CoconalaSemanticComposer(Adapter(), judge)
    result = composer({
        "thread_id": "12",
        "conversation": [{"message_id": "m1", "sent_at": "2026-09-08T00:00:00Z"}],
    })
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


def _estimate_intent():
    return {
        "action": "estimate", "thread_id": "12", "effect_key": "effect",
        "payload": {
            "title": "開発", "content": "実装", "quantity": 1,
            "price_jpy": 10000, "delivery_days": 7, "purchase_plan": "single",
            "_semantic_context_sha256": "a" * 64,
            "_request_sent_at": "2026-09-08T00:00:00Z",
            "_estimate_url": "/direct_offers/add/12", "_offer_date": "2026-09-08",
        },
    }


def test_estimate_mutation_uses_provider_ceremony_and_caches_official_receipt(monkeypatch, tmp_path):
    calls = []

    class Composer:
        def select_category(self, level, _context, _form):
            return {"master": "M", "sub": "S", "type": "T"}[level]

        def terms_with_categories(self, context, **labels):
            return {
                **context["semantic_estimate_terms"],
                "master_category_label": labels["master"],
                "sub_category_label": labels["sub"],
                "category_type_label": labels["typ"],
            }

    class Browser:
        semantic_context_required = False

        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read_thread_context(self):
            return {"conversation": [{"message_id": "m1"}]}, {"own_user_path": "/users/1"}
        def open_form(self): calls.append("open"); return {"categories": {}}
        def select_master(self, _label): calls.append("master"); return {"categories": {}}
        def select_sub(self, _label): calls.append("sub"); return {"categories": {}}
        def fill(self, _terms, _completion): calls.append("fill"); return {"selected_categories": {}}
        def read_form(self): return {}
        def first_submit(self): calls.append("first")
        def read_confirmation(self): return {}
        def fresh_thread_context(self, own): return {"own_user_path": own, "conversation": [{"message_id": "m1"}]}
        def final_submit(self, *_args, **_kwargs): calls.append("final")
        def read_after(self):
            return {"structured_offers": [{}], "own_user_path": "/users/1"}

    semantic = adapter_module.requested_estimate
    monkeypatch.setattr(semantic, "semantic_context_sha256", lambda _rows: "a" * 64)
    monkeypatch.setattr(semantic, "validate_form_contract", lambda _form: True)
    monkeypatch.setattr(semantic, "_category_type_optional", lambda _form: False)
    monkeypatch.setattr(semantic, "validate_estimate_terms", lambda terms, _context: terms)
    monkeypatch.setattr(semantic, "materialize_delivery_content", lambda terms, _today: terms)
    monkeypatch.setattr(semantic, "validate_selected_categories", lambda *_args: True)
    monkeypatch.setattr(semantic, "validate_form_selection", lambda *_args: True)
    monkeypatch.setattr(semantic, "validate_confirmation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(semantic, "completion_date", lambda *_args: __import__("datetime").date(2026, 9, 15))
    monkeypatch.setattr(semantic, "classify_delivery", lambda **_kwargs: {
        "status": "verified", "cards": [{"offer_url": "/mypage/direct_offers/55"}],
    })
    adapter = adapter_module.CoconalaReplyAdapter(
        state_root=tmp_path, inventory_reader=lambda: [],
        thread_reader=lambda _thread: ({}, {}), sender=lambda *_args: {},
        estimate_composer=Composer(), estimate_browser_factory=lambda *_args: Browser(),
    )
    intent = _estimate_intent()
    adapter.mutate(intent)
    assert calls == ["open", "master", "sub", "fill", "first", "final"]
    assert adapter.readback(intent)["provider_receipt_id"] == "/mypage/direct_offers/55"


def test_estimate_readback_finds_existing_official_card_without_mutation(monkeypatch, tmp_path):
    class Browser:
        semantic_context_required = False
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read_thread_context(self):
            return {"conversation": []}, {
                "structured_offers": [{"offer_url": "/mypage/direct_offers/55"}],
                "own_user_path": "/users/1",
            }

    semantic = adapter_module.requested_estimate
    monkeypatch.setattr(semantic, "semantic_context_sha256", lambda _rows: "a" * 64)
    monkeypatch.setattr(semantic, "materialize_delivery_content", lambda terms, _today: terms)
    monkeypatch.setattr(semantic, "classify_delivery", lambda **_kwargs: {
        "status": "already_delivered",
        "cards": [{"offer_url": "/mypage/direct_offers/55"}],
    })
    adapter = adapter_module.CoconalaReplyAdapter(
        state_root=tmp_path, inventory_reader=lambda: [],
        thread_reader=lambda _thread: ({}, {}), sender=lambda *_args: {},
        estimate_composer=object(), estimate_browser_factory=lambda *_args: Browser(),
    )
    assert adapter.readback(_estimate_intent())["provider_receipt_id"] == "/mypage/direct_offers/55"


def test_estimate_post_click_unknown_returns_for_reconciliation_without_retry_signal(monkeypatch, tmp_path):
    class Composer:
        def select_category(self, level, _context, _form):
            return {"master": "M", "sub": "S", "type": "T"}[level]
        def terms_with_categories(self, context, **labels):
            return {**context["semantic_estimate_terms"],
                    "master_category_label": labels["master"],
                    "sub_category_label": labels["sub"],
                    "category_type_label": labels["typ"]}

    class Browser:
        semantic_context_required = False
        final_clicks = 0
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read_thread_context(self): return {"conversation": []}, {"own_user_path": "/users/1"}
        def open_form(self): return {}
        def select_master(self, _label): return {}
        def select_sub(self, _label): return {}
        def fill(self, *_args): return {"selected_categories": {}}
        def read_form(self): return {}
        def first_submit(self): return None
        def read_confirmation(self): return {}
        def fresh_thread_context(self, own): return {"own_user_path": own, "conversation": []}
        def final_submit(self, *_args, **_kwargs):
            self.final_clicks += 1
            raise RuntimeError("transition_unknown")

    semantic = adapter_module.requested_estimate
    monkeypatch.setattr(semantic, "semantic_context_sha256", lambda _rows: "a" * 64)
    monkeypatch.setattr(semantic, "validate_form_contract", lambda _form: True)
    monkeypatch.setattr(semantic, "_category_type_optional", lambda _form: False)
    monkeypatch.setattr(semantic, "validate_estimate_terms", lambda terms, _context: terms)
    monkeypatch.setattr(semantic, "materialize_delivery_content", lambda terms, _today: terms)
    monkeypatch.setattr(semantic, "validate_selected_categories", lambda *_args: True)
    monkeypatch.setattr(semantic, "validate_form_selection", lambda *_args: True)
    monkeypatch.setattr(semantic, "validate_confirmation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(semantic, "completion_date", lambda *_args: __import__("datetime").date(2026, 9, 15))
    adapter = adapter_module.CoconalaReplyAdapter(
        state_root=tmp_path, inventory_reader=lambda: [],
        thread_reader=lambda _thread: ({}, {}), sender=lambda *_args: {},
        estimate_composer=Composer(), estimate_browser_factory=lambda *_args: Browser(),
    )
    assert adapter.mutate(_estimate_intent()) is None
