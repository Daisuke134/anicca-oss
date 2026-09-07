import importlib.util
from pathlib import Path


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


def test_new_buyer_event_gets_a_distinct_reply(tmp_path):
    adapter = Adapter()
    bodies = iter(("first", "second"))
    decide = lambda _context: {"action": "reply", "payload": {"body": next(bodies)}}
    reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    adapter.rows[0] = event(latest="buyer-2")
    result = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert result["effect"] == 1
    assert len(adapter.effects) == 2


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


def test_one_thread_failure_is_isolated(tmp_path):
    adapter = Adapter([event("bad", "buyer-1"), event("good", "buyer-2")])

    def decide(context):
        if context["thread_id"] == "bad":
            raise RuntimeError("model failed")
        return {"action": "estimate", "payload": {"body": "Estimate", "amount": 100}}

    result = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert result["failed"] == 1
    assert result["effect"] == 1
    assert result["items"][1]["status"] == "verified"

    replay = reply_kernel.run_wake(adapter=adapter, decide=decide, state_root=tmp_path)
    assert replay["items"][0]["reason"] == "retry_backoff"
    assert replay["items"][0]["failed"] == 0


def test_duplicate_thread_inventory_is_rejected(tmp_path):
    adapter = Adapter([event(), event()])
    try:
        reply_kernel.run_wake(adapter=adapter, decide=lambda _: {}, state_root=tmp_path)
    except ValueError as error:
        assert str(error) == "reply_inventory_duplicate"
    else:
        raise AssertionError("duplicate inventory was accepted")
