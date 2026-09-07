import importlib.util
from pathlib import Path


MODULE = Path(__file__).parents[1] / "scripts" / "reply_planner.py"
SPEC = importlib.util.spec_from_file_location("marketplace_reply_planner_test", MODULE)
planner_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(planner_module)


def row(role="buyer", reply_required=True):
    return {
        "context": {
            "reply_required": reply_required,
            "conversation": [{"role": role, "event_id": "1", "body": "hello"}],
        }
    }


def test_seller_last_and_explicit_no_reply_do_not_call_model():
    planner = planner_module.ReplyPlanner(
        lambda _context: (_ for _ in ()).throw(AssertionError("model called"))
    )
    assert planner(row("seller"))["classification"] == "awaiting_buyer"
    assert planner(row("buyer", False))["classification"] == "awaiting_buyer"


def test_buyer_last_uses_model_result():
    planner = planner_module.ReplyPlanner(lambda context: context["conversation"][0]["body"])
    assert planner(row()) == {"action": "reply", "payload": {"body": "hello"}}


def test_model_wait_and_missing_facts_are_normalized():
    assert planner_module.ReplyPlanner(lambda _context: None)(row()) == {
        "action": "noop", "classification": "no_reply"
    }

    class MissingFacts(RuntimeError):
        remaining_work = ["本人の回答"]

    def missing(_context):
        raise MissingFacts()

    assert planner_module.ReplyPlanner(missing)(row()) == {
        "action": "human",
        "reason": "reply_facts_required",
        "remaining_work": ["本人の回答"],
    }
