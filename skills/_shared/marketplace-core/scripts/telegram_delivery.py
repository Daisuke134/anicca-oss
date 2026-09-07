"""One delivery loop and one Telegram sender for every marketplace lane.

Each lane decides *what* to say. Nothing about *how* it is sent is per-platform: claim, send,
record the receipt, quarantine an abandoned claim. Lancers and CrowdWorks had grown separate copies of
that loop, and the CrowdWorks copy shipped a different transport — the openclaw CLI, which launchd
cannot find because it gives a job no PATH — so it reported nothing for a day while exiting 0.
"""

from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Optional

SHARED_TELEGRAM = Path(__file__).resolve().parents[2] / "telegram.py"


@dataclass(frozen=True)
class SendResult:
    """started=False means no provider call happened, so the claim is safe to return."""

    started: bool
    provider_id: Optional[str]
    error: Optional[str]


@dataclass(frozen=True)
class Delivery:
    attempted: int = 0
    delivered: int = 0
    delivery_uncertain: int = 0
    pre_send_failed: int = 0


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None: raise RuntimeError("shared_telegram_unavailable")
    module = importlib.util.module_from_spec(spec); sys.modules[name] = module
    spec.loader.exec_module(module); return module


def send_via_shared_client(message: str, *, chat_id: str, env_file: Optional[Path] = None) -> SendResult:
    """Send through skills/_shared/telegram.py. No CLI: a launchd job has no PATH."""
    try:
        telegram = _load("marketplace_shared_telegram", SHARED_TELEGRAM)
        client = telegram.TelegramClient.from_env(
            environ={"TELEGRAM_CHAT_ID": chat_id},
            env_file=env_file or (Path.home() / ".local/state/life-manager/.env"),
        )
        receipt = client.send_text(message)
        ids = receipt.get("message_ids") if isinstance(receipt, Mapping) else None
        provider_id = str(ids[-1]) if isinstance(ids, list) and ids else None
        return SendResult(True, provider_id, "receipt_missing" if provider_id is None else None)
    except Exception as error:
        attempted = type(error).__name__ == "TelegramDeliveryUnknown"
        return SendResult(attempted, None, "transport_unknown" if attempted else "direct_transport_unavailable")


def deliver_pending(outbox: Any, database: Path, notifier: Callable[[str], SendResult], now: str, *, limit: int = 20) -> Delivery:
    """Drain the outbox once: quarantine abandoned claims, then send what is pending."""
    attempted = delivered = uncertain = pre_send = 0
    try: outbox.reclaim_stale(Path(database))
    except Exception: pass
    for _ in range(max(1, limit)):
        item = outbox.claim_next(Path(database))
        if item is None: break
        try: result = notifier(item.message)
        except Exception: result = SendResult(True, None, "provider_error")
        # Resolve under the claim this iteration was handed. Abandoned claims are never handed to
        # another sender; the fence also protects explicit pre-send retries and future transfers.
        fence = {"claimed_at": item.claimed_at}
        try:
            if not result.started:
                outbox.mark_pre_send_failed(Path(database), item.event_key, result.error or "process_not_started", **fence); pre_send += 1
                break
            attempted += 1
            if result.provider_id:
                outbox.mark_delivered(Path(database), item.event_key, result.provider_id, now, **fence); delivered += 1
            else:
                outbox.mark_delivery_uncertain(Path(database), item.event_key, result.error or "receipt_missing", **fence); uncertain += 1
        except getattr(outbox, "StaleClaim", ()):
            break
    return Delivery(attempted, delivered, uncertain, pre_send)


__all__ = ["SendResult", "Delivery", "send_via_shared_client", "deliver_pending"]
