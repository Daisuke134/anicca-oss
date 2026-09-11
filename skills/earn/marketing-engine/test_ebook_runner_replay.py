from __future__ import annotations

from pathlib import Path

import ebook_runner
from script_ledger import ScriptLedger


def registered_script(tmp_path: Path) -> tuple[Path, str]:
    ledger_path = tmp_path / "scripts.sqlite3"
    ledger = ScriptLedger(ledger_path)
    script = {
        "schema_version": "marketing.ebook-script.v1",
        "product_id": "ebook-en",
        "account_id": "product:ebook-en",
        "language": "en",
        "version": "1",
        "parent_script_id": None,
        "source_mechanism_ids": ["mechanism-1"],
        "hook": "Pause now.",
        "pain_angle": "Noise steals attention.",
        "teaching": "Notice one breath.",
        "action": "Return gently.",
        "cta": "Read The Anicca Reset",
        "hypothesis": "A short pause improves attention.",
        "declared_mutation": "hook",
        "baseline": True,
        "campaign_id": "campaign-1",
        "creative_id": "creative-1",
        "renderer_id": "heygen-avatar-iv",
        "primary_metric": "completed_views",
        "maturity_window": "7d",
        "stop_rule": "stop after the declared window",
        "body": ("Pause now. Noise steals attention. Notice one breath. "
                 "Return gently. Read The Anicca Reset"),
    }
    return ledger_path, ledger.register(script)["script_id"]


def test_render_and_telegram_replay_each_external_effect_once(tmp_path, monkeypatch):
    ledger_path, script_id = registered_script(tmp_path)
    output = tmp_path / "video.mp4"
    renders = 0
    sends = 0

    def render(**kwargs):
        nonlocal renders
        renders += 1
        output.write_bytes(b"video")
        return {"renderer_id": "heygen-avatar-iv", "state": "rendered",
                "video_id": "video_12345678", "output": str(output),
                "sha256": "a" * 64,
                "external_effects": ["heygen_video_created", "heygen_video_downloaded"]}

    class Client:
        @classmethod
        def from_env(cls):
            return cls()

        def send_video(self, *_args, **_kwargs):
            nonlocal sends
            sends += 1
            return {"status": "delivered", "method": "sendVideo", "chat_id": 1,
                    "message_ids": [501], "date": 1}

    monkeypatch.setattr(ebook_runner, "render_heygen", render)
    monkeypatch.setattr(ebook_runner, "TelegramClient", Client)
    arguments = dict(engine=Path(ebook_runner.__file__).parent, product="ebook-en",
                     slot_at="2026-09-11T23:00:00+00:00", script_id=script_id,
                     ledger_path=ledger_path, state_root=tmp_path / "state",
                     render_output=output, telegram_preview=True)
    first = ebook_runner.run(**arguments)
    replay = ebook_runner.run(**arguments)

    assert first == replay
    assert first["state"] == "rendered"
    assert first["telegram_preview"]["message_ids"] == [501]
    assert renders == 1
    assert sends == 1


def test_unknown_telegram_delivery_is_never_sent_twice(tmp_path, monkeypatch):
    ledger_path, script_id = registered_script(tmp_path)
    output = tmp_path / "video.mp4"
    sends = 0

    def render(**kwargs):
        output.write_bytes(b"video")
        return {"renderer_id": "heygen-avatar-iv", "state": "rendered",
                "video_id": "video_12345678", "output": str(output),
                "sha256": "b" * 64,
                "external_effects": ["heygen_video_created", "heygen_video_downloaded"]}

    class Client:
        @classmethod
        def from_env(cls):
            return cls()

        def send_video(self, *_args, **_kwargs):
            nonlocal sends
            sends += 1
            raise RuntimeError("connection lost after send")

    monkeypatch.setattr(ebook_runner, "render_heygen", render)
    monkeypatch.setattr(ebook_runner, "TelegramClient", Client)
    arguments = dict(engine=Path(ebook_runner.__file__).parent, product="ebook-en",
                     slot_at="2026-09-11T23:00:00+00:00", script_id=script_id,
                     ledger_path=ledger_path, state_root=tmp_path / "state",
                     render_output=output, telegram_preview=True)
    try:
        ebook_runner.run(**arguments)
    except RuntimeError:
        pass
    else:
        raise AssertionError("unknown Telegram delivery must not become success")
    replay = ebook_runner.run(**arguments)

    assert replay["state"] == "telegram_delivery_unknown"
    assert sends == 1
