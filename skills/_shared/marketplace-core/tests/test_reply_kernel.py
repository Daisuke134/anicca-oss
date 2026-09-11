import importlib.util
from pathlib import Path
import threading


MODULE = Path(__file__).parents[1] / "scripts" / "reply_kernel.py"
SPEC = importlib.util.spec_from_file_location("marketplace_reply_kernel_test", MODULE)
reply_kernel = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(reply_kernel)


def event(thread="thread-1", latest="buyer-1"):
    return {
        "provider": "test",
        "account_id": "seller-1",
        "thread_id": thread,
        "latest_event_id": latest,
        "observed_at": "2026-09-07T00:00:00Z",
    }


class Adapter:
    def __init__(self, rows=None):
        self.rows = rows or [event()]
        self.effects = []
        self.receipts = {}

    def observe_threads(self):
        return list(self.rows)

    def observe_one(self, thread_id):
        return next(row for row in self.rows if row["thread_id"] == thread_id)

    def context(self, thread_id):
        return {"conversation": [{"role": "buyer", "body": "Hello"}]}

    def mutate(self, intent):
        self.effects.append(intent)
        self.receipts[intent["effect_key"]] = {
            "verified": True,
            "provider_receipt_id": f"message-{len(self.effects)}",
            "observed_at": "2026-09-07T00:00:01Z",
        }

    def readback(self, intent):
        return self.receipts.get(intent["effect_key"], {"authoritative_absent": True})


def test_reply_effect_is_fenced_read_back_and_replay_zero(tmp_path):
    adapter = Adapter()

    def decide(_context):
        return {"action": "reply", "payload": {"body": "Thanks"}}

    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert first["effect"] == 1
    assert first["readback"] == 1
    assert first["failed"] == 0
    assert len(adapter.effects) == 1

    second = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert second["effect"] == 0
    assert second["readback"] == 1
    assert second["items"][0]["reason"] == "replay_zero"
    assert len(adapter.effects) == 1


def test_contract_acceptance_uses_same_fence_readback_and_replay_zero(tmp_path):
    adapter = Adapter()
    decide = lambda _context: {
        "action": "accept_contract",
        "payload": {"condition_id": "condition-1", "amount": "12円"},
    }

    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)

    assert first["effect"] == 1
    assert first["readback"] == 1
    assert adapter.effects[0]["action"] == "accept_contract"
    assert replay["effect"] == 0
    assert replay["items"][0]["reason"] == "replay_zero"
    assert len(adapter.effects) == 1


def test_partial_external_action_resumes_only_after_authoritative_readback(tmp_path):
    class Partial(Adapter):
        def __init__(self):
            super().__init__()
            self.stage = "absent"

        def mutate(self, intent):
            self.effects.append(intent)
            self.stage = "partial" if self.stage == "absent" else "complete"

        def readback(self, _intent):
            if self.stage == "absent":
                return {"authoritative_absent": True}
            if self.stage == "partial":
                return {"resume_required": True}
            return {"verified": True, "provider_receipt_id": "external-1",
                    "observed_at": "2026-09-10T00:00:00Z"}

    adapter = Partial()
    decide = lambda _context: {
        "action": "external_action",
        "payload": {"kind": "schedule_meeting", "url": "https://example.com"},
    }
    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    final = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)

    assert first["pending"] == 1
    assert first["effect"] == 1
    assert replay["failed"] == 0
    assert replay["readback"] == 1
    assert replay["items"][0]["reason"] == "resumed"
    assert final["effect"] == 0
    assert final["items"][0]["reason"] == "replay_zero"
    assert len(adapter.effects) == 2



def test_decision_version_reopens_old_no_effect_state_once(tmp_path):
    adapter = Adapter()
    reply_kernel.run_wake(
        adapter=adapter,
        decide=lambda _context: {"action": "noop", "classification": "awaiting_buyer"},
        state_root=tmp_path,
    )
    adapter.rows[0]["decision_version"] = "official-actions-v1"

    result = reply_kernel.run_wake(
        adapter=adapter,
        decide=lambda _context: {
            "action": "accept_contract", "payload": {"condition_id": "condition-1"}
        },
        state_root=tmp_path,
    )

    assert result["effect"] == 1
    assert len(adapter.effects) == 1


def test_removed_decision_version_does_not_reopen_ordinary_no_effect_state(tmp_path):
    adapter = Adapter([{**event(), "decision_version": "official-actions-v1"}])
    reply_kernel.run_wake(
        adapter=adapter,
        decide=lambda _context: {"action": "noop", "classification": "awaiting_buyer"},
        state_root=tmp_path,
    )
    adapter.rows[0].pop("decision_version")
    decisions = []

    replay = reply_kernel.run_wake(
        adapter=adapter,
        decide=lambda context: decisions.append(context),
        state_root=tmp_path,
    )

    assert replay["effect"] == 0
    assert replay["items"][0]["reason"] == "replay_zero"
    assert decisions == []


def test_contract_intent_reconciles_after_provider_event_advances(tmp_path):
    class AcceptedThenInterrupted(Adapter):
        def mutate(self, intent):
            super().mutate(intent)
            self.rows[0] = event(latest="contract-event-2")
            raise RuntimeError("connection_lost_after_acceptance")

    adapter = AcceptedThenInterrupted()
    decide = lambda _context: {
        "action": "accept_contract", "payload": {"condition_id": "condition-1"}
    }
    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)

    assert first["failed"] == 1
    assert replay["effect"] == 0
    assert replay["readback"] == 1
    assert replay["items"][0]["reason"] == "replay_zero"
    assert len(adapter.effects) == 1


def test_authoritatively_absent_contract_replans_legacy_uncertain_intent(tmp_path):
    adapter = Adapter()
    row = event()
    legacy = reply_kernel._intent(row, {
        "action": "accept_contract",
        "payload": {"condition_id": "41883371", "terms_sha256": "a" * 64,
                    "title": "対象案件", "amount": "110円"},
    })
    path = reply_kernel._state_path(tmp_path, row)
    reply_kernel._write(path, {
        "version": 1, "inventory_event_id": row["latest_event_id"],
        "observation": row, "intent": legacy, "status": "reconcile_unknown",
    })
    enriched = {
        "action": "accept_contract",
        "payload": {**legacy["payload"], "client": "発注者", "worker": "Kaito"},
    }

    first = reply_kernel.run_wake(
        adapter=adapter, decide=lambda _context: enriched, state_root=tmp_path
    )
    replay = reply_kernel.run_wake(
        adapter=adapter, decide=lambda _context: enriched, state_root=tmp_path
    )

    assert first["effect"] == 1
    assert adapter.effects[0]["payload"] == enriched["payload"]
    assert replay["effect"] == 0
    assert replay["items"][0]["reason"] == "replay_zero"
    assert len(adapter.effects) == 1


def test_provider_source_gap_is_pending_without_model_or_effect(tmp_path):
    row = {**event(thread="source:gmail", latest="stale-1"),
           "pending_reason": "provider_source_stale"}
    adapter = Adapter([row])
    decisions = []

    result = reply_kernel.run_wake(
        adapter=adapter,
        decide=lambda context: decisions.append(context),
        state_root=tmp_path,
    )

    assert result["pending"] == 1
    assert result["actionable"] == 1
    assert result["items"] == [{
        "thread_id": "source:gmail", "status": "pending",
        "reason": "provider_source_stale", "effect": 0, "readback": 0, "failed": 0,
    }]
    assert decisions == []
    assert adapter.effects == []

def test_verified_effect_notifies_once_and_replay_does_not_duplicate(tmp_path):
    adapter = Adapter()
    reports = []

    def notify(intent, receipt):
        reports.append((intent["effect_key"], receipt["provider_receipt_id"]))
        return {"delivery": "delivered", "provider_message_id": "tg-1"}

    arguments = dict(
        adapter=adapter,
        decide=lambda _context: {"action": "reply", "payload": {"body": "Thanks"}},
        state_root=tmp_path,
        notify=notify,
    )
    first = reply_kernel.run_wake(**arguments)
    replay = reply_kernel.run_wake(**arguments)

    assert first["items"][0]["notification"]["delivery"] == "delivered"
    assert replay["effect"] == 0
    assert reports == [(adapter.effects[0]["effect_key"], "message-1")]

def test_new_buyer_event_gets_a_distinct_reply(tmp_path):
    adapter = Adapter()
    bodies = iter(("first", "second"))
    decide = lambda _context: {"action": "reply", "payload": {"body": next(bodies)}}
    reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    adapter.rows[0] = event(latest="buyer-2")
    result = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert result["effect"] == 1
    assert len(adapter.effects) == 2


def test_private_identity_is_hidden_from_model_and_rejected_before_provider_effect(tmp_path):
    class PrivateContext(Adapter):
        def context(self, _thread_id):
            return {
                "title": "Question for Private Legal Name",
                "conversation": [{"role": "buyer", "body": "Hello private@example.com"}],
                "grounding": {
                    "prompt_facts": [{"id": "role", "claim": "Python developer"}],
                    "private_identity_values": ["Private Legal Name", "private@example.com"],
                    "provider_public_facts": {"display_name": "Kaito｜AI自動化"},
                },
            }

    adapter = PrivateContext()
    seen = []

    def decide(row):
        seen.append(row["context"])
        return {"action": "reply", "payload": {"body": "Private Legal Nameと申します"}}

    result = reply_kernel.run_wake(
        adapter=adapter, decide=decide, state_root=tmp_path,
    )

    assert "private_identity_values" not in seen[0]["grounding"]
    assert "Private Legal Name" not in seen[0]["title"]
    assert "private@example.com" not in seen[0]["conversation"][0]["body"]
    assert result["failed"] == 1
    assert result["items"][0]["error_detail"] == "reply_private_identity_leak"
    assert adapter.effects == []


def test_verified_provider_public_name_is_allowed(tmp_path):
    class PublicContext(Adapter):
        def context(self, _thread_id):
            return {
                "conversation": [{"role": "buyer", "body": "Hello"}],
                "grounding": {
                    "private_identity_values": ["Private Legal Name"],
                    "provider_public_facts": {"display_name": "Kaito｜AI自動化"},
                },
            }

    adapter = PublicContext()
    result = reply_kernel.run_wake(
        adapter=adapter,
        decide=lambda _row: {
            "action": "reply", "payload": {"body": "Kaito｜AI自動化です"},
        },
        state_root=tmp_path,
    )

    assert result["failed"] == 0
    assert result["effect"] == 1


def test_no_effect_classification_is_replay_zero_until_source_event_changes(tmp_path):
    adapter = Adapter()
    decisions = []

    def decide(_context):
        decisions.append(True)
        return {"action": "noop", "classification": "no_reply"}

    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    adapter.rows[0] = event(latest="buyer-2")
    changed = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)

    assert first["items"][0]["reason"] == "no_effect_required"
    assert replay["items"][0]["reason"] == "replay_zero"
    assert changed["items"][0]["reason"] == "no_effect_required"
    assert len(decisions) == 2


def test_no_effect_replay_uses_inventory_fingerprint_when_official_id_differs(tmp_path):
    class DifferentOfficialId(Adapter):
        def observe_one(self, thread_id):
            row = super().observe_one(thread_id)
            return {**row, "latest_event_id": f"official-{row['latest_event_id']}"}

    adapter = DifferentOfficialId()
    decisions = []

    def decide(_context):
        decisions.append(True)
        return {"action": "noop", "classification": "no_reply"}

    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    adapter.rows[0] = event(latest="buyer-2")
    changed = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)

    assert first["items"][0]["reason"] == "no_effect_required"
    assert replay["items"][0]["reason"] == "replay_zero"
    assert changed["items"][0]["reason"] == "no_effect_required"
    assert len(decisions) == 2


def test_failure_backoff_uses_inventory_fingerprint_when_official_id_differs(tmp_path):
    class DifferentOfficialId(Adapter):
        def observe_one(self, thread_id):
            row = super().observe_one(thread_id)
            return {**row, "latest_event_id": f"official-{row['latest_event_id']}"}

    adapter = DifferentOfficialId()
    decisions = []

    def decide(_context):
        decisions.append(True)
        raise RuntimeError("temporary")

    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)

    assert first["failed"] == 1
    assert replay["failed"] == 0
    assert replay["items"][0]["reason"] == "retry_backoff"
    assert len(decisions) == 1


def test_human_gate_is_durable_pending_and_does_not_block_another_thread(tmp_path):
    adapter = Adapter([event("human", "buyer-1"), event("ready", "buyer-2")])

    def decide(context):
        if context["thread_id"] == "human":
            return {
                "action": "human",
                "reason": "person_bound_interview",
                "remaining_work": ["Complete the official interview"],
            }
        return {"action": "reply", "payload": {"body": "Ready"}}

    result = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert result["pending"] == 1
    assert result["effect"] == 1
    assert result["failed"] == 0
    assert [effect["thread_id"] for effect in adapter.effects] == ["ready"]


def test_human_gate_notification_is_durable_deduplicated_and_keeps_scanning(tmp_path):
    adapter = Adapter([event("human", "buyer-1"), event("ready", "buyer-2")])
    notices = []

    def decide(context):
        if context["thread_id"] == "human":
            return {
                "action": "human",
                "reason": "person_bound_interview",
                "remaining_work": ["Complete the official interview"],
                "handoff": {
                    "title": "Japanese evaluator",
                    "url": "https://work.mercor.com/jobs/list_1",
                    "deadline": "公式期限表示なし",
                },
            }
        return {"action": "reply", "payload": {"body": "Ready"}}

    def human_notify(row, decision):
        notices.append((row["thread_id"], decision["handoff"]["url"]))
        return {"delivery": "delivered", "provider_message_id": "tg-1"}

    arguments = {
        "adapter": adapter,
        "decide": decide,
        "state_root": tmp_path,
        "human_notify": human_notify,
    }
    first = reply_kernel.run_wake(**arguments)
    replay = reply_kernel.run_wake(**arguments)

    assert first["pending"] == replay["pending"] == 1
    assert first["effect"] == 1
    assert replay["effect"] == 0
    assert notices == [("human", "https://work.mercor.com/jobs/list_1")]
    state = reply_kernel._load(next(
        path for path in tmp_path.glob("threads/*/state.json")
        if reply_kernel._load(path).get("status") == "waiting_human"
    ))
    assert state["human_notification"]["provider_message_id"] == "tg-1"


def test_one_thread_failure_is_isolated(tmp_path):
    adapter = Adapter([event("bad", "buyer-1"), event("good", "buyer-2")])

    def decide(context):
        if context["thread_id"] == "bad":
            raise RuntimeError("model failed")
        return {"action": "estimate", "payload": {"body": "Estimate", "amount": 100}}

    result = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert result["failed"] == 1
    assert result["items"][0]["error_detail"] == "model failed"
    assert result["effect"] == 1
    assert result["items"][1]["status"] == "verified"

    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert replay["items"][0]["reason"] == "retry_backoff"
    assert replay["items"][0]["failed"] == 0


def test_authoritative_provider_rejection_becomes_durable_external_wait(tmp_path):
    class Restricted(Adapter):
        def mutate(self, _intent):
            raise RuntimeError("submit_rejected_sending_unavailable")

        def classify_mutation_error(self, error):
            assert str(error) == "submit_rejected_sending_unavailable"
            return {
                "reason": "provider_sending_unavailable",
                "remaining_work": ["Wait for the provider message control to become available"],
            }

    result = reply_kernel.run_wake(
        adapter=Restricted(),
        decide=lambda _context: {"action": "reply", "payload": {"body": "Thanks"}},
        state_root=tmp_path,
    )

    assert result["failed"] == 0
    assert result["pending"] == 1
    assert result["effect"] == 0
    assert result["items"][0]["reason"] == "provider_sending_unavailable"


def test_duplicate_thread_inventory_is_rejected(tmp_path):
    adapter = Adapter([event(), event()])
    try:
        reply_kernel.run_wake(adapter=adapter, decide=lambda _: {}, state_root=tmp_path)
    except ValueError as error:
        assert str(error) == "reply_inventory_duplicate"
    else:
        raise AssertionError("duplicate inventory was accepted")


def test_single_worker_keeps_thread_affine_adapter_on_calling_thread(tmp_path):
    owner = threading.get_ident()

    class ThreadAffine(Adapter):
        def _same(self):
            if threading.get_ident() != owner:
                raise RuntimeError("wrong_thread")

        def observe_threads(self):
            self._same()
            return super().observe_threads()

        def observe_one(self, thread_id):
            self._same()
            return super().observe_one(thread_id)

        def context(self, thread_id):
            self._same()
            return super().context(thread_id)

    result = reply_kernel.run_wake(
        adapter=ThreadAffine(),
        decide=lambda _row: {"action": "noop", "classification": "no_reply"},
        state_root=tmp_path,
        max_workers=1,
    )
    assert result["failed"] == 0
    assert result["items"][0]["status"] == "no_reply"


def test_delivery_unknown_never_blindly_replays_same_intent(tmp_path):
    class Unknown(Adapter):
        def mutate(self, intent):
            self.effects.append(intent)

        def readback(self, _intent):
            return {"authoritative_absent": True}

    adapter = Unknown()
    decide = lambda _row: {"action": "reply", "payload": {"body": "one"}}
    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert first["effect"] == 1
    assert first["items"][0]["reason"] == "reconcile_unknown"
    second = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert second["effect"] == 0
    assert second["items"][0]["reason"] == "reconcile_unknown"
    assert len(adapter.effects) == 1


def test_readback_exception_after_intent_preserves_reconcile_fence(tmp_path):
    class ReadbackBreaksAfterEffect(Adapter):
        def __init__(self):
            super().__init__()
            self.broken = True

        def readback(self, intent):
            if self.effects and self.broken:
                raise RuntimeError("provider_dom_changed")
            return super().readback(intent)

    adapter = ReadbackBreaksAfterEffect()
    decide = lambda _row: {"action": "reply", "payload": {"body": "one"}}
    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert first["failed"] == 1
    assert len(adapter.effects) == 1
    state_path = next(tmp_path.glob("threads/*/state.json"))
    state = reply_kernel._load(state_path)
    assert state["status"] == "reconcile_unknown"
    assert state["intent"]["payload"]["body"] == "one"

    adapter.broken = False
    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert replay["failed"] == 0
    assert replay["effect"] == 0
    assert replay["items"][0]["reason"] == "replay_zero"
    assert len(adapter.effects) == 1


def test_classified_observation_error_before_intent_is_pending_with_backoff(tmp_path):
    class TemporarilyUnreadable(Adapter):
        def observe_one(self, _thread_id):
            raise RuntimeError("navigation_timeout")

        def classify_observation_error(self, error):
            if str(error) == "navigation_timeout":
                return {"reason": "provider_readback_temporarily_unavailable"}
            return None

    result = reply_kernel.run_wake(
        adapter=TemporarilyUnreadable(),
        decide=lambda _row: {"action": "noop", "classification": "no_reply"},
        state_root=tmp_path,
    )

    assert result["failed"] == 0
    assert result["pending"] == 1
    assert result["items"][0]["reason"] == "provider_readback_temporarily_unavailable"
    state = reply_kernel._load(next(tmp_path.glob("threads/*/state.json")))
    assert state["status"] == "retry_wait"
    assert state["retry_count"] == 1


def test_classified_readback_error_after_effect_stays_pending_and_never_replays(tmp_path):
    class TemporarilyUnreadableAfterEffect(Adapter):
        def __init__(self):
            super().__init__()
            self.broken = True

        def readback(self, intent):
            if self.effects and self.broken:
                raise RuntimeError("navigation_timeout")
            return super().readback(intent)

        def classify_observation_error(self, error):
            if str(error) == "navigation_timeout":
                return {"reason": "provider_readback_temporarily_unavailable"}
            return None

    adapter = TemporarilyUnreadableAfterEffect()
    decide = lambda _row: {"action": "reply", "payload": {"body": "one"}}
    first = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)

    assert first["failed"] == 0
    assert first["pending"] == 1
    assert first["items"][0]["reason"] == "provider_readback_temporarily_unavailable"
    assert len(adapter.effects) == 1
    state_path = next(tmp_path.glob("threads/*/state.json"))
    assert reply_kernel._load(state_path)["status"] == "reconcile_unknown"

    adapter.broken = False
    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert replay["failed"] == 0
    assert replay["effect"] == 0
    assert replay["items"][0]["reason"] == "replay_zero"
    assert len(adapter.effects) == 1


def test_chat_id_reads_declared_provider_config_without_repo_literal(tmp_path):
    config = tmp_path / "telegram.env"
    config.write_text("CROWDWORKS_REPORT_CHAT=operator-chat\n", encoding="utf-8")
    assert reply_kernel._chat_id("", config) == "operator-chat"
    assert reply_kernel._chat_id("explicit", config) == "explicit"


def test_pre_effect_readback_must_prove_authoritative_absence(tmp_path):
    class UnknownBeforeEffect(Adapter):
        def readback(self, _intent):
            return {"authoritative_absent": False}

    adapter = UnknownBeforeEffect()
    result = reply_kernel.run_wake(
        adapter=adapter,
        decide=lambda _row: {"action": "reply", "payload": {"body": "one"}},
        state_root=tmp_path,
    )
    assert result["effect"] == 0
    assert result["pending"] == 1
    assert result["items"][0]["reason"] == "pre_effect_reconcile_unknown"
    assert adapter.effects == []
