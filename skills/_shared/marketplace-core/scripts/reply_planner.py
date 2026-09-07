#!/usr/bin/env python3
"""Provider-neutral model-facing planner for marketplace Reply."""

from __future__ import annotations

from typing import Any, Callable, Mapping


class ReplyPlanner:
    """Turn normalized conversation context into a shared-kernel decision.

    ``compose`` remains an LLM-backed callable. Deterministic code only owns the
    universal state transition around its result; it never judges buyer text.
    """

    def __init__(self, compose: Callable[[dict[str, Any]], str | None]):
        self.compose = compose

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
        if context.get("reply_required") is False or latest["role"] != "buyer":
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
        if not isinstance(body, str) or not body.strip():
            raise ValueError("reply_body_invalid")
        return {"action": "reply", "payload": {"body": body.strip()}}
