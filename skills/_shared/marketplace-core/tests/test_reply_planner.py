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


def test_official_provider_action_precedes_message_composition():
    value = row("seller", reply_required=False)
    value["context"]["required_action"] = {
        "action": "accept_contract",
        "payload": {"condition_id": "41879089", "amount": "12円"},
    }
    planner = planner_module.ReplyPlanner(
        lambda _context: (_ for _ in ()).throw(AssertionError("model called"))
    )

    assert planner(value) == value["context"]["required_action"]


def test_external_action_uses_the_same_structured_decision_contract():
    value = row("seller", reply_required=False)
    value["context"]["required_action"] = {
        "action": "external_action",
        "payload": {"kind": "schedule_meeting", "url": "https://example.com/booking"},
    }
    planner = planner_module.ReplyPlanner(
        lambda _context: (_ for _ in ()).throw(AssertionError("model called"))
    )

    assert planner(value) == value["context"]["required_action"]


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


def test_structured_reply_and_estimate_use_one_decision_contract():
    reply = planner_module.ReplyPlanner(lambda _context: {
        "action": "reply", "payload": {"body": "承知しました。"},
    })
    assert reply(row()) == {
        "action": "reply", "payload": {"body": "承知しました。"},
    }
    estimate = planner_module.ReplyPlanner(lambda _context: {
        "action": "estimate",
        "payload": {"title": "開発", "amount": 10000, "currency": "JPY"},
    })
    assert estimate(row()) == {
        "action": "estimate",
        "payload": {"title": "開発", "amount": 10000, "currency": "JPY"},
    }


def test_structured_wait_is_normalized_and_invalid_effect_is_rejected():
    wait = planner_module.ReplyPlanner(lambda _context: {
        "action": "wait", "reason": "official_context_required",
        "remaining_work": ["公式応募条件を取得"],
    })
    assert wait(row()) == {
        "action": "wait", "reason": "official_context_required",
        "remaining_work": ["公式応募条件を取得"],
    }
    invalid = planner_module.ReplyPlanner(lambda _context: {
        "action": "estimate", "payload": {},
    })
    try:
        invalid(row())
    except ValueError as error:
        assert str(error) == "reply_payload_invalid"
    else:
        raise AssertionError("empty estimate payload was accepted")


def test_validated_semantic_judgement_projects_to_shared_actions():
    planner = planner_module.ReplyPlanner(lambda _context: {
        "next_action": "send_estimate",
        "estimate_terms": {"title": "開発", "price_jpy": 10000},
    })
    assert planner(row()) == {
        "action": "estimate", "payload": {"title": "開発", "price_jpy": 10000},
    }
    clarify = planner_module.ReplyPlanner(lambda _context: {
        "next_action": "clarify", "reply_body": "納期をご指定ください。",
    })
    assert clarify(row()) == {
        "action": "reply", "payload": {"body": "納期をご指定ください。"},
    }
    stop = planner_module.ReplyPlanner(lambda _context: {"next_action": "stop"})
    assert stop(row()) == {"action": "noop", "classification": "closed"}


def test_semantic_wait_uses_uncertainty_or_becomes_no_reply():
    blocked = planner_module.ReplyPlanner(lambda _context: {
        "next_action": "wait", "uncertainty": ["公式応募条件"],
    })
    assert blocked(row()) == {
        "action": "wait", "reason": "official_context_required",
        "remaining_work": ["公式応募条件"],
    }
    idle = planner_module.ReplyPlanner(lambda _context: {
        "next_action": "wait", "uncertainty": [],
    })
    assert idle(row()) == {"action": "noop", "classification": "no_reply"}


def test_provider_can_require_cumulative_model_judgement_for_seller_last_debt():
    value = row("seller", reply_required=False)
    value["context"]["decision_required"] = True
    planner = planner_module.ReplyPlanner(lambda _context: {
        "next_action": "send_estimate",
        "estimate_terms": {"title": "開発", "price_jpy": 10000},
    })
    assert planner(value)["action"] == "estimate"


def test_missing_fact_decision_never_becomes_a_customer_reply():
    planner = planner_module.ReplyPlanner(lambda _context: {
        "next_action": "wait",
        "uncertainty": ["性別"],
    })
    assert planner(row()) == {
        "action": "wait",
        "reason": "official_context_required",
        "remaining_work": ["性別"],
    }
