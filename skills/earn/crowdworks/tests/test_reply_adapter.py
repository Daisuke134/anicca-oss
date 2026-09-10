from pathlib import Path
import importlib.util
import sys


MODULE = Path(__file__).parents[1] / "scripts/reply_adapter.py"
SPEC = importlib.util.spec_from_file_location("crowdworks_reply_adapter_test", MODULE)
adapter_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter_module
SPEC.loader.exec_module(adapter_module)


def test_observation_uses_official_thread_and_message_ids():
    row = {"thread_id": 303996182, "id": 425906697, "proposal_status": "proposed"}
    observed = adapter_module.CrowdWorksReplyAdapter._observation(row)
    assert observed["provider"] == "crowdworks"
    assert observed["thread_id"] == "303996182"
    assert observed["latest_event_id"] == "425906697"
    assert observed["decision_version"] == "official-actions-v3"


def test_only_officially_proposed_threads_reopen_old_no_effect_state():
    ordinary = adapter_module.CrowdWorksReplyAdapter._observation(
        {"thread_id": 1, "id": 2, "proposal_status": "rejected"}
    )
    assert "decision_version" not in ordinary


def test_provider_route_requires_exact_crowdworks_origin_and_path():
    route = adapter_module.CrowdWorksReplyAdapter._provider_route
    assert route("https://crowdworks.jp/contracts/63570481#scroll_to_message") == (
        "contracts", "63570481"
    )
    assert route("https://evil.example/contracts/63570481") is None
    assert route("https://crowdworks.jp/contracts/63570481/anything") is None
    assert route("https://crowdworks.jp/messages/1?next=/contracts/63570481") is None


def test_owner_enters_shared_reply_kernel():
    owner = MODULE.with_name("reply-owner").read_text(encoding="utf-8")
    assert "marketplace-core/scripts/reply_kernel.py" in owner
    assert "reply_adapter.py" in owner


def test_adapter_contains_no_provider_lifecycle_copy():
    source = MODULE.read_text(encoding="utf-8")
    assert "def run_wake" not in source
    assert "next_eligible_at" not in source
    assert "effect_key" not in source


def test_collapsed_message_uses_full_body_not_visible_digest():
    source = MODULE.read_text(encoding="utf-8")
    assert "find(item => item.querySelector('p'))" in source
    assert "getComputedStyle(item).display" not in source


def test_contract_acceptance_stays_in_provider_adapter():
    source = MODULE.read_text(encoding="utf-8")
    assert '"action": "accept_contract"' in source
    assert 'a.intro-employer_proposed_project[href="#message-dialog-agreement"]' in source
    assert 'input[name="check-terms"]' in source
    assert 'input[value="同意して契約する"]' in source


class _Locator:
    def __init__(self, *, count=1, visible=True, disabled=False, action=None, terms=None,
                 children=None, text="", nested=None):
        self._count = count
        self._visible = visible
        self._disabled = disabled
        self._action = action
        self._terms = terms
        self._children = children or []
        self._text = text
        self._nested = nested
        self.clicked = 0
        self.checked = 0

    def count(self): return self._count
    def is_visible(self): return self._visible
    def is_disabled(self): return self._disabled
    def get_attribute(self, _name): return self._action
    def locator(self, _selector): return self._nested or self
    def nth(self, index): return self._children[index]
    def evaluate_all(self, _script): return dict(self._terms or {})
    def inner_text(self): return self._text
    def click(self): self.clicked += 1
    def check(self): self.checked += 1


class _Page:
    def __init__(self, mapping, title="対象案件【クラウドワークス】",
                 url="https://crowdworks.jp/proposals/message-1"):
        self.mapping = mapping
        self._title = title
        self.url = url
    def locator(self, selector): return self.mapping[selector]
    def wait_for_load_state(self, *_args, **_kwargs): pass
    def title(self): return self._title


def _contract_adapter(*, status="proposed", amount="12円", trigger_count=1):
    adapter = adapter_module.CrowdWorksReplyAdapter({})
    adapter.rows = {"thread-1": {
        "thread_id": "thread-1", "id": "message-1", "proposal_status": status,
    }}
    trigger = _Locator(count=trigger_count)
    form = _Locator(
        action="/proposal_conditions/41879089/agree",
        terms={"タイトル（仕事名）": "対象案件", "クライアント（発注者）": "発注者",
               "ワーカー（受注者）": "Kaito｜AI自動化", "金額": amount},
    )
    checkbox = _Locator()
    submit = _Locator()
    adapter.page = _Page({
        'a.intro-employer_proposed_project[href="#message-dialog-agreement"]': trigger,
        'form[action^="/proposal_conditions/"][action$="/agree"]': form,
        'input[name="check-terms"]': checkbox,
        'input[value="同意して契約する"]': submit,
        'a[href^="/contracts/"]': _Locator(count=0),
        'div.progress_detail': _Locator(count=1, nested=_Locator(count=0)),
        'table.conditions.recent_condition': _Locator(
            text="発注者 » Kaito｜AI自動化 固定報酬: 12円"
        ),
    })
    adapter._open_thread_page = lambda _thread_id: None
    adapter._detail = lambda _thread_id: []
    return adapter, trigger, checkbox, submit


def test_contract_action_requires_one_official_proposed_control_and_fingerprints_terms():
    adapter, _, _, _ = _contract_adapter()
    action = adapter._contract_action("thread-1")
    assert action["action"] == "accept_contract"
    assert action["payload"]["condition_id"] == "41879089"
    assert action["payload"]["title"] == "対象案件"
    assert action["payload"]["client"] == "発注者"
    assert action["payload"]["worker"] == "Kaito｜AI自動化"
    assert action["payload"]["amount"] == "12円"
    assert len(action["payload"]["terms_sha256"]) == 64

    not_proposed, _, _, _ = _contract_adapter(status="rejected")
    assert not_proposed._contract_action("thread-1") is None
    ambiguous, _, _, _ = _contract_adapter(trigger_count=2)
    assert ambiguous._contract_action("thread-1") is None


def test_contract_mutation_rejects_changed_terms_before_click():
    adapter, trigger, _, _ = _contract_adapter()
    intent = {"action": "accept_contract", "thread_id": "thread-1",
              "payload": adapter._contract_action("thread-1")["payload"]}
    adapter.page.mapping[
        'form[action^="/proposal_conditions/"][action$="/agree"]'
    ]._terms["金額"] = "1円"

    try:
        adapter.mutate(intent)
    except RuntimeError as error:
        assert str(error) == "crowdworks_contract_terms_changed"
    else:
        raise AssertionError("changed contract terms were accepted")
    assert trigger.clicked == 0


def test_pre_effect_readback_accepts_legacy_persisted_payload_shape():
    adapter, _, _, _ = _contract_adapter()
    current = adapter._contract_action("thread-1")["payload"]
    legacy = {field: current[field] for field in (
        "condition_id", "terms_sha256", "title", "amount"
    )}

    assert adapter.readback({"action": "accept_contract", "thread_id": "thread-1",
                             "payload": legacy}) == {"authoritative_absent": True}


def test_contract_mutation_checks_terms_and_submits_once():
    adapter, trigger, checkbox, submit = _contract_adapter()
    intent = {"action": "accept_contract", "thread_id": "thread-1",
              "payload": adapter._contract_action("thread-1")["payload"]}

    adapter.mutate(intent)

    assert trigger.clicked == 1
    assert checkbox.checked == 1
    assert submit.clicked == 1


def test_contract_readback_requires_one_visible_official_contract_link():
    adapter, _, _, _ = _contract_adapter(status="contracted")
    contract_link = _Locator(action="/contracts/987654")
    adapter.page.mapping['div.progress_detail'] = _Locator(
        count=1, nested=_Locator(count=1, children=[contract_link])
    )
    adapter.page.mapping[
        'a.intro-employer_proposed_project[href="#message-dialog-agreement"]'
    ] = _Locator(count=0)
    receipt = adapter.readback({"action": "accept_contract", "thread_id": "thread-1",
                                "payload": {"title": "対象案件", "amount": "12円",
                                            "client": "発注者", "worker": "Kaito｜AI自動化"}})
    assert receipt["verified"] is True
    assert receipt["provider_receipt_id"] == "contract:987654"

    uncertain, _, _, _ = _contract_adapter(status="unknown")
    uncertain.page.mapping[
        'a.intro-employer_proposed_project[href="#message-dialog-agreement"]'
    ] = _Locator(count=0)
    assert uncertain.readback({"action": "accept_contract", "thread_id": "thread-1",
                               "payload": {"title": "対象案件", "amount": "12円"}}) == {}


def test_contract_readback_accepts_exact_message_redirect_to_matching_contract():
    adapter, _, _, _ = _contract_adapter(status="contracted")
    adapter.page.url = "https://crowdworks.jp/contracts/63570481#scroll_to_message"
    adapter.page.mapping[
        'a.intro-employer_proposed_project[href="#message-dialog-agreement"]'
    ] = _Locator(count=0)
    adapter.page.mapping["body"] = _Locator(
        text="契約名 対象案件 クライアント 発注者 契約金額（税込） 12円 Kaito｜AI自動化"
    )
    payload = {"title": "対象案件", "amount": "12円", "client": "発注者",
               "worker": "Kaito｜AI自動化"}

    receipt = adapter.readback({"action": "accept_contract", "thread_id": "thread-1",
                                "payload": payload})

    assert receipt["provider_receipt_id"] == "contract:63570481"


def test_contract_readback_ignores_page_wide_or_mismatched_contract_links():
    adapter, _, _, _ = _contract_adapter(status="contracted")
    adapter.page.mapping[
        'a.intro-employer_proposed_project[href="#message-dialog-agreement"]'
    ] = _Locator(count=0)
    adapter.page.mapping['a[href^="/contracts/"]'] = _Locator(
        count=1, children=[_Locator(action="/contracts/111")]
    )
    assert adapter.readback({"action": "accept_contract", "thread_id": "thread-1",
                             "payload": {"title": "対象案件", "amount": "12円"}}) == {}

    adapter.page.mapping['div.progress_detail'] = _Locator(
        count=1, nested=_Locator(
            count=1, children=[_Locator(action="/contracts/987654")]
        )
    )
    adapter.page.mapping['table.conditions.recent_condition'] = _Locator(
        text="発注者 » Kaito｜AI自動化 固定報酬: 999円"
    )
    assert adapter.readback({"action": "accept_contract", "thread_id": "thread-1",
                             "payload": {"title": "対象案件", "amount": "12円",
                                         "client": "発注者"}}) == {}


def test_contract_readback_verifies_worker_acceptance_while_client_is_pending():
    adapter, _, _, _ = _contract_adapter(status="proposed")
    adapter.page.mapping[
        'a.intro-employer_proposed_project[href="#message-dialog-agreement"]'
    ] = _Locator(count=0)
    adapter.page.mapping['div.progress_detail'] = _Locator(
        count=1, text=("まだクライアントが契約に同意していません。"
                       "クライアントが契約に同意すると契約成立となります。"),
        nested=_Locator(count=0),
    )
    payload = {"condition_id": "41879089", "title": "対象案件", "amount": "12円",
               "client": "発注者", "worker": "Kaito｜AI自動化"}

    receipt = adapter.readback({"action": "accept_contract", "thread_id": "thread-1",
                                "payload": payload})

    assert receipt["verified"] is True
    assert receipt["provider_receipt_id"] == "condition-accepted:41879089"


def test_contract_readback_does_not_require_reply_composer_after_agreement():
    adapter, _, _, _ = _contract_adapter(status="proposed")
    opened = []
    adapter._open_thread_page = opened.append
    adapter._detail = lambda _thread_id: (_ for _ in ()).throw(
        AssertionError("contract readback must not require the reply composer")
    )
    adapter.page.mapping[
        'a.intro-employer_proposed_project[href="#message-dialog-agreement"]'
    ] = _Locator(count=0)
    adapter.page.mapping['div.progress_detail'] = _Locator(
        count=1, text=("まだクライアントが契約に同意していません。"
                       "クライアントが契約に同意すると契約成立となります。"),
        nested=_Locator(count=0),
    )
    payload = {"condition_id": "41879089", "title": "対象案件", "amount": "12円",
               "client": "発注者", "worker": "Kaito｜AI自動化"}

    receipt = adapter.readback({"action": "accept_contract", "thread_id": "thread-1",
                                "payload": payload})

    assert opened == ["thread-1"]
    assert receipt["provider_receipt_id"] == "condition-accepted:41879089"


def test_single_thread_observation_does_not_require_reply_composer():
    adapter, _, _, _ = _contract_adapter(status="proposed")
    opened = []
    adapter._open_thread_page = opened.append
    adapter._detail = lambda _thread_id: (_ for _ in ()).throw(
        AssertionError("official thread observation must not require the reply composer")
    )

    observation = adapter.observe_one("thread-1")

    assert opened == ["thread-1"]
    assert observation["thread_id"] == "thread-1"
    assert observation["decision_version"] == "official-actions-v3"


def test_buyer_google_form_becomes_shared_external_action():
    adapter = adapter_module.CrowdWorksReplyAdapter({})
    adapter.rows = {"thread-1": {
        "thread_id": "thread-1", "id": "message-1", "proposal_status": "proposed",
    }}
    adapter.conversations = {"thread-1": [{
        "event_id": "event-1", "role": "buyer", "sender": "buyer",
        "sent_at": "2026-09-10T00:00:00Z", "body": "フォームへ回答してください",
        "links": ["https://forms.gle/AbCdEf123"],
    }]}

    action = adapter._external_form_action("thread-1")

    assert action["action"] == "external_action"
    assert action["payload"]["kind"] == "submit_google_form"
    assert action["payload"]["url"] == "https://forms.gle/AbCdEf123"
    assert len(action["payload"]["url_sha256"]) == 64
    assert "回答" in action["payload"]["completion_body"]


def test_acknowledgement_after_google_form_keeps_external_action_outstanding():
    adapter = adapter_module.CrowdWorksReplyAdapter({})
    adapter.conversations = {"thread-1": [
        {"event_id": "buyer-1", "role": "buyer", "sender": "buyer",
         "sent_at": "2026-09-10T00:00:00Z", "body": "フォームへ回答してください",
         "links": ["https://forms.gle/AbCdEf123"]},
        {"event_id": "seller-1", "role": "seller", "sender": "seller",
         "sent_at": "2026-09-10T00:01:00Z", "body": "回答を進めます",
         "links": []},
    ]}

    action = adapter._external_form_action("thread-1")

    assert action["action"] == "external_action"
    assert action["payload"]["url"] == "https://forms.gle/AbCdEf123"


def test_external_form_action_rejects_untrusted_or_ambiguous_links():
    adapter = adapter_module.CrowdWorksReplyAdapter({})
    adapter.rows = {"thread-1": {"thread_id": "thread-1", "id": "message-1"}}
    base = {"event_id": "event-1", "role": "buyer", "sender": "buyer",
            "sent_at": "2026-09-10T00:00:00Z", "body": "回答してください"}
    adapter.conversations = {"thread-1": [{**base, "links": ["https://evil.example/form"]}]}
    assert adapter._external_form_action("thread-1") is None
    adapter.conversations = {"thread-1": [{**base, "links": [
        "https://forms.gle/one", "https://docs.google.com/forms/d/e/two/viewform",
    ]}]}
    assert adapter._external_form_action("thread-1") is None


def test_google_form_answers_bind_current_metadata_to_private_profiles(tmp_path):
    candidate = tmp_path / "candidate.json"
    provider = tmp_path / "provider.json"
    candidate.write_text(__import__("json").dumps({
        "candidate": {"application_email": "private@example.com",
                      "name_romaji_parts": {"family": "Narita", "given": "Daisuke"}},
        "facts": [{"claim": "Nara Institute graduate"},
                  {"claim": "Mitsubishi UFJ employment"}],
    }), encoding="utf-8")
    provider.write_text(__import__("json").dumps({
        "display_name": "Public Seller", "provider_employee_id": "7145638",
    }), encoding="utf-8")
    adapter = adapter_module.CrowdWorksReplyAdapter(
        {}, candidate_profile=candidate,
        provider_profile=__import__("json").loads(provider.read_text(encoding="utf-8")),
    )
    items = [
        {"title": "①クラウドワークスのユーザー名を教えてください", "type": 0,
         "entries": [{"id": 10, "choices": [], "required": True}]},
        {"title": "③本名のイニシャルを教えてください", "type": 0,
         "entries": [{"id": 11, "choices": [], "required": True}]},
        {"title": "職務経歴【1】の開始（入社）した時期を教えてください", "type": 9,
         "entries": [{"id": 12, "choices": [], "required": True}]},
    ]

    assert adapter._question_answers(items) == [
        ("entry.10", "Public Seller"), ("entry.11", "ND"),
        ("entry.12_year", "2025"), ("entry.12_month", "04"),
        ("entry.12_day", "01"), ("emailAddress", "private@example.com"),
    ]


def test_google_form_readback_resumes_message_without_resubmitting_form(tmp_path):
    state = tmp_path / "reply" / "state.json"
    adapter = adapter_module.CrowdWorksReplyAdapter({}, state_path=state)
    url_hash = "a" * 64
    receipt = adapter._form_receipt_path(url_hash)
    receipt.parent.mkdir(parents=True)
    receipt.write_text(__import__("json").dumps({
        "url_sha256": url_hash, "confirmation_sha256": "b" * 64,
    }), encoding="utf-8")
    body = "Googleフォームへの回答を完了しました。"
    intent = {"action": "external_action", "thread_id": "thread-1", "payload": {
        "kind": "submit_google_form", "url_sha256": url_hash, "completion_body": body,
    }}
    adapter._detail = lambda _thread: []
    assert adapter.readback(intent) == {"resume_required": True}
    adapter._detail = lambda _thread: [{
        "event_id": "message-1", "role": "seller", "body": body,
    }]
    result = adapter.readback(intent)
    assert result["verified"] is True
    assert result["provider_receipt_id"].endswith(":message-1")


def test_google_form_readback_proves_absence_only_before_prepared_marker(tmp_path):
    state = tmp_path / "reply" / "state.json"
    adapter = adapter_module.CrowdWorksReplyAdapter({}, state_path=state)
    url_hash = "a" * 64
    intent = {"action": "external_action", "thread_id": "thread-1", "payload": {
        "kind": "submit_google_form", "url_sha256": url_hash,
        "completion_body": "Googleフォームへの回答を完了しました。",
    }}

    assert adapter.readback(intent) == {"authoritative_absent": True}

    receipt = adapter._form_receipt_path(url_hash)
    receipt.parent.mkdir(parents=True)
    receipt.write_text(__import__("json").dumps({
        "status": "prepared", "url_sha256": url_hash,
    }), encoding="utf-8")
    adapter._detail = lambda _thread: []
    assert adapter.readback(intent) == {"resume_required": True}


def test_prepared_google_form_requests_confirmation_once_and_accepts_buyer_receipt(tmp_path):
    state = tmp_path / "reply" / "state.json"
    adapter = adapter_module.CrowdWorksReplyAdapter({}, state_path=state)
    url_hash = "a" * 64
    receipt = adapter._form_receipt_path(url_hash)
    receipt.parent.mkdir(parents=True)
    receipt.write_text(__import__("json").dumps({
        "status": "prepared", "url_sha256": url_hash,
    }), encoding="utf-8")
    intent = {"action": "external_action", "thread_id": "thread-1", "payload": {
        "kind": "submit_google_form", "url_sha256": url_hash,
        "completion_body": "Googleフォームへの回答を完了しました。",
    }}
    sent = []
    adapter._open = lambda: None
    adapter._send_reply_once = lambda thread, body: sent.append((thread, body))

    adapter.mutate(intent)
    assert sent == [("thread-1", adapter.FORM_CONFIRMATION_BODY)]
    persisted = __import__("json").loads(receipt.read_text(encoding="utf-8"))
    assert persisted["status"] == "confirmation_requested"
    assert persisted["confirmation_thread_id"] == "thread-1"
    sent.clear()
    adapter.mutate(intent)
    assert sent == [("thread-1", adapter.FORM_CONFIRMATION_BODY)]
    sent.clear()
    adapter.mutate({**intent, "thread_id": "thread-2"})
    assert sent == []

    adapter._detail = lambda _thread: [{
        "event_id": "seller-1", "role": "seller", "body": adapter.FORM_CONFIRMATION_BODY,
    }]
    assert adapter.readback(intent) == {}
    adapter._detail = lambda _thread: [{
        "event_id": "seller-1", "role": "seller", "body": adapter.FORM_CONFIRMATION_BODY,
    }, {
        "event_id": "buyer-2", "role": "buyer", "body": "回答を確認しました。",
    }]
    verified = adapter.readback(intent)
    assert verified["verified"] is True
    assert verified["provider_receipt_id"] == "google-form-buyer-confirmed:buyer-2"

    other = {**intent, "thread_id": "thread-2"}
    adapter._detail = lambda _thread: (_ for _ in ()).throw(
        AssertionError("the shared form confirmation belongs to one thread")
    )
    assert adapter.readback(other) == {}


def test_google_form_confirmation_rejects_unrelated_and_negated_buyer_messages(tmp_path):
    state = tmp_path / "reply" / "state.json"
    adapter = adapter_module.CrowdWorksReplyAdapter({}, state_path=state)
    url_hash = "a" * 64
    receipt = adapter._form_receipt_path(url_hash)
    receipt.parent.mkdir(parents=True)
    receipt.write_text(__import__("json").dumps({
        "status": "confirmation_requested", "url_sha256": url_hash,
        "confirmation_thread_id": "thread-1",
    }), encoding="utf-8")
    intent = {"action": "external_action", "thread_id": "thread-1", "payload": {
        "kind": "submit_google_form", "url_sha256": url_hash,
        "completion_body": "Googleフォームへの回答を完了しました。",
    }}
    prefix = [{"event_id": "seller-1", "role": "seller",
               "body": adapter.FORM_CONFIRMATION_BODY}]
    for body in ("日程を確認しました。", "フォームを確認しました。",
                 "確認しましたが、回答は届いていません。"):
        adapter._detail = lambda _thread, body=body: prefix + [{
            "event_id": "buyer-2", "role": "buyer", "body": body,
        }]
        assert adapter.readback(intent) == {}
