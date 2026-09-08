from __future__ import annotations

import importlib.util
import inspect
import json
import sqlite3
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "capafy_company_receipt.py"
GOAL_MONITOR = Path(__file__).parents[1] / "capafy-goal-monitor.sh"


def load_module():
    spec = importlib.util.spec_from_file_location("capafy_company_receipt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def sources() -> dict:
    return {
        "inventory": {
            "readable": True,
            "counts": {"total": 32, "listed": 22, "occupied": 3, "free": 2, "retry": 7, "blocked": 0, "unknown": 0},
        },
        "candidate": {
            "candidate_id": "capafy-o13-interviews",
            "title": "Interview Synthesizer",
            "content_sha256": "sha256:" + "a" * 64,
            "state": "ready",
            "platform_state": "not_submitted",
        },
        "marketing": {
            "telegram_message_id": "27263",
            "outcome": {
                "agent_id": "7785270416",
                "title": "Data Analyst",
                "reel_url": "https://www.instagram.com/reel/abc/",
                "media_sha256": "sha256:" + "b" * 64,
                "owner_session_verified": True,
            },
        },
        "money": {
            "observed_at": "2026-08-22T10:00:00Z",
            "orders": 5,
            "money": {"gross_usd": "19.98", "one_time_revenue_usd": None, "pending_usd": "8.00", "realized_usd": "0.00", "refunds_usd": "0.00", "settled_mrr_usd": None, "net_mrr_usd": None},
            "money_status": {"settled_mrr_usd": "unknown_no_seller_subscription_source"},
        },
        "growth": {
            "signal": "sales",
            "company_orders": 1,
            "winner": {"agent_id": "6839055303", "name": "Academic Humanizer"},
            "attribution_status": "official_seller_ranking",
        },
    }


def test_receipt_joins_skill_slots_post_money_under_one_run_id() -> None:
    module = load_module()
    receipt = module.build_receipt(sources(), "2026-08-22T11:00:00Z")

    assert receipt["run_id"].startswith("capafy-")
    assert receipt["skill"]["candidate_id"] == "capafy-o13-interviews"
    assert receipt["slots"]["occupied"] == 3
    assert receipt["distribution"][0]["native_url"].endswith("/abc/")
    assert receipt["money"]["gross_usd"] == "19.98"
    assert receipt["money"]["settled_mrr_usd"] is None
    assert receipt["growth_signal"] == {
        "signal": "sales",
        "company_orders": 1,
        "winner_agent_id": "6839055303",
        "attribution_status": "official_seller_ranking",
    }
    assert receipt["telegram"] == {"status": "pending", "message_id": None}


def test_semantic_replay_has_same_run_id_but_new_state_changes_it() -> None:
    module = load_module()
    first = module.build_receipt(sources(), "2026-08-22T11:00:00Z")
    replay = module.build_receipt(sources(), "2026-08-22T12:00:00Z")
    changed_sources = sources()
    changed_sources["inventory"]["counts"]["occupied"] = 4
    changed = module.build_receipt(changed_sources, "2026-08-22T12:00:00Z")

    assert first["run_id"] == replay["run_id"]
    assert changed["run_id"] != first["run_id"]


def test_delivery_sends_exact_state_once_and_persists_message_id(tmp_path: Path) -> None:
    module = load_module()
    calls = []

    def sender(message: str) -> str:
        calls.append(message)
        return "9001"

    receipt = module.build_receipt(sources(), "2026-08-22T11:00:00Z")
    first = module.deliver_receipt(receipt, tmp_path / "outbox.sqlite", tmp_path / "receipts", sender)
    replay = module.deliver_receipt(receipt, tmp_path / "outbox.sqlite", tmp_path / "receipts", sender)

    assert calls == [module.render_message(receipt)]
    assert first["telegram"] == {"status": "delivered", "message_id": "9001"}
    assert replay == first
    persisted = json.loads((tmp_path / "receipts" / f"{receipt['run_id']}.json").read_text())
    assert persisted["telegram"]["message_id"] == "9001"


def test_provider_without_message_id_is_quarantined_and_not_retried(tmp_path: Path) -> None:
    module = load_module()
    calls = 0

    def sender(_message: str) -> str:
        nonlocal calls
        calls += 1
        raise module.DeliveryUncertain("message_id_missing")

    receipt = module.build_receipt(sources(), "2026-08-22T11:00:00Z")
    first = module.deliver_receipt(receipt, tmp_path / "outbox.sqlite", tmp_path / "receipts", sender)
    replay = module.deliver_receipt(receipt, tmp_path / "outbox.sqlite", tmp_path / "receipts", sender)

    assert calls == 1
    assert first["telegram"]["status"] == "delivery_uncertain"
    assert replay == first


def test_direct_sender_returns_message_id_once_and_uses_life_manager_env(tmp_path: Path, monkeypatch) -> None:
    state_home = tmp_path / "life-manager"
    state_home.mkdir()
    (state_home / ".env").write_text("TELEGRAM_BOT_TOKEN=fixture-token\n", encoding="utf-8")
    monkeypatch.setenv("LIFE_MANAGER_STATE_HOME", str(state_home))
    monkeypatch.setenv("CAPAFY_TELEGRAM_TARGET", "fixture-chat")
    module = load_module()
    calls = []

    class StubTelegramClient:
        @classmethod
        def from_env(cls, *, environ, env_file):
            calls.append(("from_env", environ, env_file))
            return cls()

        def send_text(self, text, *, chat_id):
            calls.append(("send_text", text, chat_id))
            return {"status": "delivered", "message_ids": [9001]}

    monkeypatch.setattr(module, "TelegramClient", StubTelegramClient)
    assert module._telegram_sender("fixture report") == "9001"
    assert calls[0][0] == "from_env"
    assert calls[0][1]["TELEGRAM_CHAT_ID"] == "fixture-chat"
    assert calls[0][2] == state_home / ".env"
    assert calls[1:] == [
        ("send_text", "fixture report", "fixture-chat"),
    ]


@pytest.mark.parametrize("error_kind", ("transport_unknown", "provider_error"))
def test_direct_transport_or_provider_error_quarantines_once_and_replay_does_not_retry(
    tmp_path: Path, monkeypatch, error_kind: str
) -> None:
    state_home = tmp_path / "life-manager"
    state_home.mkdir()
    monkeypatch.setenv("LIFE_MANAGER_STATE_HOME", str(state_home))
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "fixture-chat")
    module = load_module()
    calls = 0

    class StubTelegramClient:
        @classmethod
        def from_env(cls, *, environ, env_file):
            assert environ["TELEGRAM_CHAT_ID"] == "fixture-chat"
            assert env_file == state_home / ".env"
            return cls()

        def send_text(self, _text, *, chat_id):
            nonlocal calls
            assert chat_id == "fixture-chat"
            calls += 1
            if error_kind == "transport_unknown":
                raise module.TelegramDeliveryUnknown("transport lost")
            raise module.TelegramError("provider rejected", error_code=400)

    monkeypatch.setattr(module, "TelegramClient", StubTelegramClient)
    receipt = module.build_receipt(sources(), "2026-08-22T11:00:00Z")
    outbox = tmp_path / "outbox.sqlite"
    receipts = tmp_path / "receipts"
    first = module.deliver_receipt(receipt, outbox, receipts, module._telegram_sender)
    replay = module.deliver_receipt(receipt, outbox, receipts, module._telegram_sender)

    assert calls == 1
    assert first["telegram"] == {"status": "delivery_uncertain", "message_id": None}
    assert replay == first
    with sqlite3.connect(outbox) as db:
        row = db.execute("SELECT status, attempt_count, provider_message_id, last_error_code FROM telegram_outbox").fetchone()
    expected_error = "sender_delivery_unknown" if error_kind == "transport_unknown" else "sender_provider_error_400"
    assert row == ("delivery_uncertain", 1, None, expected_error)


def test_direct_sender_has_no_openclaw_call_or_subprocess() -> None:
    module = load_module()
    source = inspect.getsource(module._telegram_sender).lower()

    assert "openclaw message send" not in source
    assert "subprocess" not in source


@pytest.mark.parametrize("outbox_status", ("delivery_uncertain", "delivered"))
def test_replay_recovers_missing_receipt_from_authoritative_outbox(tmp_path: Path, outbox_status: str) -> None:
    module = load_module()
    receipt = module.build_receipt(sources(), "2026-08-22T11:00:00Z")
    outbox = tmp_path / "outbox.sqlite"
    receipts = tmp_path / "receipts"
    message = module.render_message(receipt)
    assert module.enqueue(outbox, receipt["run_id"], message, receipt["observed_at"]) is True
    assert module.claim_next(outbox) is not None
    if outbox_status == "delivery_uncertain":
        module.mark_delivery_uncertain(outbox, receipt["run_id"], "sender_timeout")
    else:
        module.mark_delivered(outbox, receipt["run_id"], "9002", "2026-08-22T11:01:00Z")

    def sender(_message: str) -> str:
        raise AssertionError("exact replay must not call provider")

    recovered = module.deliver_receipt(receipt, outbox, receipts, sender)

    expected = {
        "status": "delivered" if outbox_status == "delivered" else "delivery_uncertain",
        "message_id": "9002" if outbox_status == "delivered" else None,
    }
    assert recovered["telegram"] == expected
    receipt_path = receipts / f"{receipt['run_id']}.json"
    if outbox_status == "delivered":
        persisted = json.loads(receipt_path.read_text())
        assert persisted["telegram"] == expected
    else:
        assert not receipt_path.exists()


def test_uncertain_replay_cannot_overwrite_existing_delivered_receipt(tmp_path: Path, monkeypatch) -> None:
    module = load_module()
    receipt = module.build_receipt(sources(), "2026-08-22T11:00:00Z")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    receipt_path = receipts / f"{receipt['run_id']}.json"
    delivered = {**receipt, "telegram": {"status": "delivered", "message_id": "9003"}}
    receipt_path.write_text(json.dumps(delivered), encoding="utf-8")
    outbox = tmp_path / "outbox.sqlite"
    module.enqueue(outbox, receipt["run_id"], module.render_message(receipt), receipt["observed_at"])

    class _Uncertain:
        event_key = receipt["run_id"]
        status = "delivery_uncertain"
        provider_message_id = None

    monkeypatch.setattr(module, "list_items", lambda _database: [_Uncertain()])
    replay = module.deliver_receipt(receipt, outbox, receipts, lambda _message: pytest.fail("must not send"))

    assert replay["telegram"] == delivered["telegram"]
    assert json.loads(receipt_path.read_text())["telegram"] == delivered["telegram"]


def test_hourly_goal_monitor_uses_unified_receipt_before_shared_sender() -> None:
    source = GOAL_MONITOR.read_text()
    hourly = source.index('CAPAFY_REPORT_KIND:-morning')
    reconcile = source.index("capafy_hourly_reconcile.py", hourly)
    receipt = source.index("capafy_company_receipt.py", reconcile)
    shared = source.index("skills/_shared/send-telegram.sh", receipt)

    assert hourly < reconcile < receipt < shared
    assert 'if [ "$REPORT_KIND" = "hourly" ]' in source
    assert 'exit "$UNIFIED_RC"' in source
