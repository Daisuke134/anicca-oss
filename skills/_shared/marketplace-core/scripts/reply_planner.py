#!/usr/bin/env python3
"""Provider-neutral model-facing planner for marketplace Reply."""

from __future__ import annotations

from typing import Any, Callable, Mapping


class ReplyPlanner:
    """Turn normalized conversation context into a shared-kernel decision.

    ``compose`` remains an LLM-backed callable. Deterministic code only owns the
    universal state transition around its result; it never judges buyer text.
    """

    def __init__(self, compose: Callable[[dict[str, Any]], str | Mapping[str, Any] | None]):
        self.compose = compose

    @staticmethod
    def _structured(value: Mapping[str, Any]) -> dict[str, Any]:
        action = value.get("action")
        if action is None:
            action = value.get("next_action")
        if action == "send_estimate":
            terms = value.get("estimate_terms")
            if not isinstance(terms, Mapping) or not terms:
                raise ValueError("reply_payload_invalid")
            return {"action": "estimate", "payload": dict(terms)}
        if action == "clarify":
            body = value.get("reply_body")
            if not isinstance(body, str) or not body.strip():
                raise ValueError("reply_payload_invalid")
            return {"action": "reply", "payload": {"body": body.strip()}}
        if action == "stop":
            return {"action": "noop", "classification": "closed"}
        if action == "reply" and "payload" not in value and "reply_body" in value:
            body = value.get("reply_body")
            if not isinstance(body, str) or not body.strip():
                raise ValueError("reply_payload_invalid")
            return {"action": "reply", "payload": {"body": body.strip()}}
        if action == "wait" and "reason" not in value:
            remaining = value.get("uncertainty")
            if not isinstance(remaining, list) or not remaining:
                return {"action": "noop", "classification": "no_reply"}
            return {
                "action": "wait",
                "reason": "official_context_required",
                "remaining_work": remaining,
            }
        if action in {"reply", "estimate"}:
            payload = value.get("payload")
            if not isinstance(payload, Mapping) or not payload:
                raise ValueError("reply_payload_invalid")
            return {"action": action, "payload": dict(payload)}
        if action == "noop":
            classification = value.get("classification")
            if classification not in {"awaiting_buyer", "closed", "no_reply", "noop"}:
                raise ValueError("reply_noop_classification_invalid")
            return {"action": "noop", "classification": classification}
        if action in {"wait", "human"}:
            reason = value.get("reason")
            remaining = value.get("remaining_work")
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("reply_wait_reason_invalid")
            if not isinstance(remaining, list) or not remaining or not all(
                isinstance(item, str) and item.strip() for item in remaining
            ):
                raise ValueError("remaining_work_invalid")
            result = {
                "action": action,
                "reason": reason.strip(),
                "remaining_work": [item.strip() for item in remaining],
            }
            if action == "human":
                handoff = value.get("handoff")
                if not isinstance(handoff, Mapping):
                    raise ValueError("reply_human_handoff_invalid")
                if any(
                    not isinstance(handoff.get(field), str) or not handoff[field].strip()
                    for field in ("title", "url", "deadline")
                ):
                    raise ValueError("reply_human_handoff_invalid")
                result["handoff"] = {
                    field: handoff[field].strip()
                    for field in ("title", "url", "deadline")
                }
            return result
        raise ValueError("reply_action_invalid")

    def __call__(self, row: dict[str, Any]) -> dict[str, Any]:
        context = row.get("context")
        if not isinstance(context, Mapping):
            raise ValueError("reply_context_invalid")
        conversation = context.get("conversation")
        if not isinstance(conversation, list) or not conversation:
            raise ValueError("reply_conversation_invalid")
        latest = conversation[-1]
        if not isinstance(latest, Mapping) or latest.get("role") not in {"buyer", "seller"}:
            raise ValueError("reply_conversation_invalid")
        if context.get("decision_required") is not True and (
            context.get("reply_required") is False or latest["role"] != "buyer"
        ):
            return {"action": "noop", "classification": "awaiting_buyer"}
        try:
            body = self.compose(dict(context))
        except Exception as error:
            remaining = getattr(error, "remaining_work", None)
            if not isinstance(remaining, list) or not remaining or not all(
                isinstance(item, str) and item.strip() for item in remaining
            ):
                raise
            return {
                "action": "human",
                "reason": "reply_facts_required",
                "remaining_work": [item.strip() for item in remaining],
            }
        if body is None:
            return {"action": "noop", "classification": "no_reply"}
        if isinstance(body, Mapping):
            return self._structured(body)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("reply_body_invalid")
        return {"action": "reply", "payload": {"body": body.strip()}}
