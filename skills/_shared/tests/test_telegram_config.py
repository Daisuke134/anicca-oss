from pathlib import Path

from skills._shared.telegram import load_config


def test_alert_chat_id_is_the_portable_default(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_ALERT_CHAT_ID=12345\n",
        encoding="utf-8",
    )

    assert load_config(environ={}, env_file=env_file) == ("test-token", "12345")


def test_explicit_chat_id_wins_over_alert_chat_id(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_CHAT_ID=primary\n"
        "TELEGRAM_ALERT_CHAT_ID=fallback\n",
        encoding="utf-8",
    )

    assert load_config(environ={}, env_file=env_file) == ("test-token", "primary")
