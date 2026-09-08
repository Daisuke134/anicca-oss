import importlib.util
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_shared_effect_notification_delivers_once_and_replays_zero(tmp_path):
    notification = load("test_effect_notification", "effect_notification.py")
    calls = []

    def sender(message):
        calls.append(message)
        delivery = load("test_effect_delivery", "telegram_delivery.py")
        return delivery.SendResult(True, "provider-1", None)

    arguments = dict(
        database=tmp_path / "outbox.sqlite3",
        event_key="platform:lane:effect-1",
        message="Codex::: effect happened",
        observed_at="2026-09-07T05:00:00Z",
        chat_id="123",
        env_file=tmp_path / "telegram.env",
        sender=sender,
    )
    first = notification.notify_effect(**arguments)
    replay = notification.notify_effect(**arguments)

    assert first["delivery"] == "delivered"
    assert first["provider_message_id"] == "provider-1"
    assert replay["delivery"] == "delivered"
    assert replay["attempted"] == 0
    assert calls == ["Codex::: effect happened"]


def test_delivered_event_replays_receipt_when_generated_wording_drifts(tmp_path):
    notification = load("test_effect_notification_wording_drift", "effect_notification.py")
    calls = []

    def sender(message):
        calls.append(message)
        delivery = load("test_effect_delivery_wording_drift", "telegram_delivery.py")
        return delivery.SendResult(True, "provider-1", None)

    arguments = dict(
        database=tmp_path / "outbox.sqlite3",
        event_key="platform:lane:effect-1",
        observed_at="2026-09-07T05:00:00Z",
        chat_id="123",
        env_file=tmp_path / "telegram.env",
        sender=sender,
    )
    first = notification.notify_effect(message="Codex::: first wording", **arguments)
    replay = notification.notify_effect(message="Codex::: revised wording", **arguments)

    assert first["provider_message_id"] == "provider-1"
    assert replay == {
        "event_key": "platform:lane:effect-1",
        "delivery": "delivered",
        "provider_message_id": "provider-1",
        "attempted": 0,
        "delivered": 0,
        "delivery_uncertain": 0,
        "pre_send_failed": 0,
    }
    assert calls == ["Codex::: first wording"]
