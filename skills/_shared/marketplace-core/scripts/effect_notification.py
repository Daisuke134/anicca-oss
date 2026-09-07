"""One receipt-backed notification path for every marketplace effect."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Optional


HERE = Path(__file__).resolve().parent


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def notify_effect(
    *,
    database: Path,
    event_key: str,
    message: str,
    observed_at: str,
    chat_id: str,
    env_file: Path,
    sender: Optional[Callable[[str], Any]] = None,
    repeat_after_seconds: Optional[float] = 3600,
) -> dict[str, Any]:
    """Enqueue, deliver and return the durable provider receipt for one effect.

    Event identity and wording belong to the calling lane. Claiming, at-most-once
    delivery, receipt persistence and retry state are shared by every provider.
    """
    outbox = _load("marketplace_effect_outbox", HERE / "telegram_outbox.py")
    delivery = _load("marketplace_effect_delivery", HERE / "telegram_delivery.py")
    outbox.enqueue(
        Path(database), event_key, message, observed_at,
        repeat_after_seconds=repeat_after_seconds,
    )
    current = next(
        item for item in outbox.list_items(Path(database))
        if item.event_key == event_key
    )
    if current.status != "pending":
        return {
            "event_key": event_key,
            "delivery": current.status,
            "provider_message_id": current.provider_message_id,
            "attempted": 0,
            "delivered": 0,
            "delivery_uncertain": 0,
            "pre_send_failed": 0,
        }
    notifier = sender or (
        lambda body: delivery.send_via_shared_client(
            body, chat_id=chat_id, env_file=Path(env_file)
        )
    )
    outcome = delivery.deliver_pending(
        outbox, Path(database), notifier, observed_at, limit=1
    )
    item = next(
        item for item in outbox.list_items(Path(database))
        if item.event_key == event_key
    )
    return {
        "event_key": event_key,
        "delivery": item.status,
        "provider_message_id": item.provider_message_id,
        "attempted": outcome.attempted,
        "delivered": outcome.delivered,
        "delivery_uncertain": outcome.delivery_uncertain,
        "pre_send_failed": outcome.pre_send_failed,
    }


__all__ = ["notify_effect"]
