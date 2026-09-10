from pathlib import Path
import importlib.util
import sys


MODULE = Path(__file__).parents[1] / "scripts/reply_adapter.py"
SPEC = importlib.util.spec_from_file_location("crowdworks_reply_adapter_test", MODULE)
adapter_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter_module
SPEC.loader.exec_module(adapter_module)


def test_observation_uses_official_thread_and_message_ids():
    row = {"thread_id": 303996182, "id": 425906697}
    observed = adapter_module.CrowdWorksReplyAdapter._observation(row)
    assert observed["provider"] == "crowdworks"
    assert observed["thread_id"] == "303996182"
    assert observed["latest_event_id"] == "425906697"


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
    def __init__(self, *, count=1, visible=True, disabled=False, action=None, terms=None):
        self._count = count
        self._visible = visible
        self._disabled = disabled
        self._action = action
        self._terms = terms
        self.clicked = 0
        self.checked = 0

    def count(self): return self._count
    def is_visible(self): return self._visible
    def is_disabled(self): return self._disabled
    def get_attribute(self, _name): return self._action
    def locator(self, _selector): return self
    def evaluate_all(self, _script): return dict(self._terms or {})
    def click(self): self.clicked += 1
    def check(self): self.checked += 1


class _Page:
    def __init__(self, mapping): self.mapping = mapping
    def locator(self, selector): return self.mapping[selector]
    def wait_for_load_state(self, *_args, **_kwargs): pass


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
    })
    adapter._detail = lambda _thread_id: []
    return adapter, trigger, checkbox, submit


def test_contract_action_requires_one_official_proposed_control_and_fingerprints_terms():
    adapter, _, _, _ = _contract_adapter()
    action = adapter._contract_action("thread-1")
    assert action["action"] == "accept_contract"
    assert action["payload"]["condition_id"] == "41879089"
    assert action["payload"]["title"] == "対象案件"
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


def test_contract_mutation_checks_terms_and_submits_once():
    adapter, trigger, checkbox, submit = _contract_adapter()
    intent = {"action": "accept_contract", "thread_id": "thread-1",
              "payload": adapter._contract_action("thread-1")["payload"]}

    adapter.mutate(intent)

    assert trigger.clicked == 1
    assert checkbox.checked == 1
    assert submit.clicked == 1


def test_contract_readback_requires_official_contracted_status():
    adapter, _, _, _ = _contract_adapter(status="contracted")
    adapter.observe_threads = lambda: []
    receipt = adapter.readback({"action": "accept_contract", "thread_id": "thread-1"})
    assert receipt["verified"] is True
    assert receipt["provider_receipt_id"] == "contract:thread-1:message-1"

    uncertain, _, _, _ = _contract_adapter(status="unknown")
    uncertain.observe_threads = lambda: []
    assert uncertain.readback({"action": "accept_contract", "thread_id": "thread-1"}) == {}
