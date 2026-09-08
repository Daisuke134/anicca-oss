#!/usr/bin/env python3
"""Join Capafy business state under one run_id and deliver it at most once."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOX_MODULES = REPO_ROOT / "skills/_shared/marketplace-core/scripts"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(OUTBOX_MODULES))
from telegram_outbox import enqueue, claim_next, list_items, mark_delivered, mark_delivery_uncertain  # noqa: E402
from skills._shared.telegram import TelegramClient, TelegramDeliveryUnknown, TelegramError  # noqa: E402


STATE_HOME = Path(os.environ.get("LIFE_MANAGER_STATE_HOME", Path.home() / ".local/state/life-manager")).expanduser()
CAPAFY_STATE = STATE_HOME / "state"
DEFAULT_OUTBOX = STATE_HOME / "state/capafy-telegram-outbox.sqlite"
DEFAULT_RECEIPTS = STATE_HOME / "state/capafy-company-receipts"


class DeliveryUncertain(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _semantic_payload(sources: dict) -> dict:
    inventory = sources.get("inventory") or {}
    candidate = sources.get("candidate") or {}
    marketing = sources.get("marketing") or {}
    outcome = marketing.get("outcome") if isinstance(marketing, dict) else {}
    outcome = outcome if isinstance(outcome, dict) else {}
    money_source = sources.get("money") or {}
    growth = sources.get("growth") or {}
    return {
        "skill": {
            "candidate_id": candidate.get("candidate_id"),
            "name": candidate.get("title"),
            "agent_id": candidate.get("agent_id"),
            "version": candidate.get("version"),
            "remote_status": candidate.get("platform_state"),
            "content_sha256": candidate.get("content_sha256"),
        },
        "slots": copy.deepcopy(inventory.get("counts") or {}),
        "distribution": [
            {
                "platform": "instagram",
                "skill_agent_id": outcome.get("agent_id"),
                "skill_name": outcome.get("title"),
                "native_url": outcome.get("reel_url"),
                "creative_sha256": outcome.get("media_sha256"),
                "owner_session_verified": outcome.get("owner_session_verified"),
            }
        ],
        "money": copy.deepcopy(money_source.get("money") or {}),
        "money_status": copy.deepcopy(money_source.get("money_status") or {}),
        "orders": money_source.get("orders"),
        "growth_signal": {
            "signal": growth.get("signal") or "unknown",
            "company_orders": growth.get("company_orders"),
            "winner_agent_id": ((growth.get("winner") or {}).get("agent_id") or (growth.get("winner") or {}).get("agentId")) if isinstance(growth.get("winner"), dict) else None,
            "attribution_status": growth.get("attribution_status"),
        },
    }


def build_receipt(sources: dict, observed_at: str) -> dict:
    semantic = _semantic_payload(sources)
    digest = hashlib.sha256(_canonical(semantic)).hexdigest()
    receipt = {
        "schema_version": 1,
        "kind": "capafy_company_receipt",
        "run_id": f"capafy-{digest[:24]}",
        "state_sha256": f"sha256:{digest}",
        "observed_at": observed_at,
        **semantic,
        "telegram": {"status": "pending", "message_id": None},
    }
    if not isinstance(receipt["slots"].get("occupied"), int):
        raise ValueError("slot occupancy is not readable")
    if not isinstance(receipt["skill"].get("candidate_id"), str):
        raise ValueError("candidate identity is missing")
    if "settled_mrr_usd" not in receipt["money"]:
        raise ValueError("settled_mrr_usd must remain explicit")
    return receipt


def render_message(receipt: dict) -> str:
    skill = receipt["skill"]
    slots = receipt["slots"]
    money = receipt["money"]
    distribution = receipt["distribution"][0]
    growth = receipt.get("growth_signal") or {}
    post = distribution.get("native_url") or "none"
    mrr = money.get("settled_mrr_usd")
    mrr_text = "unknown" if mrr is None else f"${mrr}"
    return (
        f"Codex::: Capafy company receipt {receipt['run_id']}\n"
        f"Candidate: {skill.get('name')} ({skill.get('candidate_id')}) status={skill.get('remote_status')}\n"
        f"Slots: occupied={slots.get('occupied')} free={slots.get('free')} retry={slots.get('retry')} listed={slots.get('listed')}\n"
        f"Latest Reel: {post}\n"
        f"Money: orders={receipt.get('orders')} gross=${money.get('gross_usd')} pending=${money.get('pending_usd')} realized=${money.get('realized_usd')} refunds=${money.get('refunds_usd')} settled MRR={mrr_text}\n"
        f"Growth signal: {growth.get('signal')} (winner_agent_id={growth.get('winner_agent_id') or 'none'}; attribution={growth.get('attribution_status') or 'none'})"
    )


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def deliver_receipt(
    receipt: dict,
    outbox_database: Path,
    receipts_directory: Path,
    sender: Callable[[str], str],
) -> dict:
    run_id = receipt["run_id"]
    receipt_path = receipts_directory / f"{run_id}.json"
    message = render_message(receipt)
    inserted = enqueue(outbox_database, run_id, message, receipt["observed_at"])
    if not inserted:
        if receipt_path.exists():
            return json.loads(receipt_path.read_text())
        outbox_item = next((item for item in list_items(outbox_database) if item.event_key == run_id), None)
        if outbox_item is not None:
            recovered = copy.deepcopy(receipt)
            if outbox_item.status == "delivered" and outbox_item.provider_message_id:
                recovered["telegram"] = {"status": "delivered", "message_id": outbox_item.provider_message_id}
                _atomic_write(receipt_path, recovered)
            else:
                recovered["telegram"] = {"status": "delivery_uncertain", "message_id": None}
            return recovered
        return receipt
    claimed = claim_next(outbox_database)
    if claimed is None or claimed.event_key != run_id:
        raise RuntimeError("outbox claim did not return the enqueued run")
    delivered = copy.deepcopy(receipt)
    try:
        message_id = str(sender(message))
        if not message_id or "\x00" in message_id:
            raise DeliveryUncertain("message_id_missing")
    except DeliveryUncertain as exc:
        mark_delivery_uncertain(outbox_database, run_id, str(exc) or "delivery_uncertain")
        delivered["telegram"] = {"status": "delivery_uncertain", "message_id": None}
        _atomic_write(receipt_path, delivered)
        return delivered
    delivered_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    mark_delivered(outbox_database, run_id, message_id, delivered_at)
    delivered["telegram"] = {"status": "delivered", "message_id": message_id}
    _atomic_write(receipt_path, delivered)
    return delivered


def _find_message_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("messageId", "message_id"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int)) and str(candidate):
                return str(candidate)
        for child in value.values():
            found = _find_message_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_message_id(child)
            if found:
                return found
    return None


def _telegram_sender(message: str) -> str:
    """Send one receipt through the shared direct Telegram Bot API client."""
    target = (os.environ.get("CAPAFY_TELEGRAM_TARGET") or os.environ.get("TELEGRAM_ALERT_CHAT_ID")
              or os.environ.get("LM_TELEGRAM_ALERT_CHAT_ID"))
    if not target:
        raise DeliveryUncertain("telegram_target_missing")
    try:
        env_file = STATE_HOME / ".env"
        client_environment = dict(os.environ)
        client_environment["TELEGRAM_CHAT_ID"] = target
        telegram = TelegramClient.from_env(
            environ=client_environment,
            env_file=env_file,
        )
        response = telegram.send_text(message, chat_id=target)
    except TelegramDeliveryUnknown as exc:
        raise DeliveryUncertain("sender_delivery_unknown") from exc
    except TelegramError as exc:
        suffix = f"_{exc.error_code}" if exc.error_code is not None else ""
        raise DeliveryUncertain(f"sender_provider_error{suffix}") from exc

    if not isinstance(response, dict):
        raise DeliveryUncertain("message_id_missing")
    message_ids = response.get("message_ids")
    if not isinstance(message_ids, list):
        raise DeliveryUncertain("message_id_missing")
    for candidate in message_ids:
        if isinstance(candidate, bool) or not isinstance(candidate, (str, int)):
            continue
        message_id = str(candidate).strip()
        if message_id and "\x00" not in message_id:
            return message_id
    raise DeliveryUncertain("message_id_missing")


def _load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"source is not an object: {path}")
    return value


def _live_sources() -> dict:
    inventory_result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "skills/capafy-autopublish/scripts/inventory_status.py")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    inventory = json.loads(inventory_result.stdout.splitlines()[-1])
    backlog = _load(STATE_HOME / "state/capafy-candidate-backlog.json")
    candidates = [item for item in backlog.get("items", []) if item.get("state") in {"ready", "submitted", "listed"}]
    if not candidates:
        raise ValueError("candidate backlog has no receipt candidate")
    candidate = sorted(candidates, key=lambda item: item["candidate_id"])[0]
    marketing = _load(CAPAFY_STATE / "capafy-marketing-terminal.json")
    outcome = marketing.get("outcome") or {}
    media_path = Path(str(outcome.get("media_path") or ""))
    if media_path.is_file():
        outcome["media_sha256"] = "sha256:" + hashlib.sha256(media_path.read_bytes()).hexdigest()
    money = _load(STATE_HOME / "state/capafy-hourly-reconcile.json")
    growth_path = STATE_HOME / "state/capafy-sales-ranking.json"
    growth = _load(growth_path) if growth_path.is_file() else {"signal": "unknown"}
    return {"inventory": inventory, "candidate": candidate, "marketing": marketing, "money": money, "growth": growth}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deliver", nargs="?")
    parser.add_argument("--outbox", type=Path, default=DEFAULT_OUTBOX)
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    parser.add_argument("--observed-at")
    args = parser.parse_args(argv)
    observed_at = args.observed_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    receipt = build_receipt(_live_sources(), observed_at)
    delivered = deliver_receipt(receipt, args.outbox, args.receipts, _telegram_sender)
    print(json.dumps(delivered, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if delivered["telegram"]["status"] == "delivered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
