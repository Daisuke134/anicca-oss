import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from investment_status import ALPACA_SIGNUP_URL, build_investment_reply, build_investment_status


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_in_review_reuses_existing_paper_receipts(tmp_path):
    root = tmp_path / "alpaca-investment"
    _write(root / "account-status.json", {"application_status": "in_review"})
    _write(root / "observation-latest.json", {"account": {"equity": "99996.76", "cash": "99996.76"}})
    _write(root / "allocation-latest.json", {"approved": False, "reason": "No fresh edge."})

    message = build_investment_status(tmp_path)

    assert message.startswith("Investment Loop\n")
    assert "審査中" in message
    assert "今は操作不要" in message
    assert "資産 $99996.76" in message
    assert "取引なし" in message
    assert "No fresh edge." in message
    assert "運転: 不明" in message
    assert "Alpaca Loop" not in message
    assert "Codex" not in message


def test_unknown_status_fails_closed_without_inventing_balance(tmp_path):
    root = tmp_path / "alpaca-investment"
    root.mkdir()
    (root / "account-status.json").write_text("not-json", encoding="utf-8")

    reply = build_investment_reply(tmp_path)
    message = reply["text"]

    assert "状態をまだ確認できません" in message
    assert "ライブ注文は出しません" in message
    assert "最新snapshotをまだ読み取れません" in message
    assert "reply_markup" not in reply


def test_unknown_daily_pnl_is_written_as_unknown_not_none(tmp_path):
    _write(tmp_path / "alpaca-investment" / "account-status.json", {"application_status": "active"})
    live = tmp_path / "alpaca-investment-shadow"
    _write(live / "observation-latest.json", {
        "mode": "shadow", "account": {"equity": "66.72", "cash": "0"}})
    _write(live / "allocation-latest.json", {"approved": False, "reason": "安全確認中"})
    _write(live / "risk-latest.json", {"official_pnl_ny_day_usd": None})

    message = build_investment_status(tmp_path)

    assert "日次純損益: 不明" in message
    assert "None" not in message


def test_missing_account_status_offers_exact_official_signup_link(tmp_path):
    reply = build_investment_reply(tmp_path)

    assert reply["text"].startswith("Investment Loop\n")
    assert "口座開設と本人確認" in reply["text"]
    assert "Life Managerと同じメールアドレス" in reply["text"]
    buttons = reply["reply_markup"]["inline_keyboard"][0]
    assert buttons == [
        {"text": "Alpacaで口座開設する", "url": ALPACA_SIGNUP_URL},
        {"text": "今はしない", "callback_data": "invest:later"},
    ]


def test_known_account_status_never_reopens_signup(tmp_path):
    _write(tmp_path / "alpaca-investment" / "account-status.json", {"application_status": "in_review"})

    reply = build_investment_reply(tmp_path)

    assert "審査中" in reply["text"]
    assert "reply_markup" not in reply


def test_active_account_is_presented_as_live_ready_but_still_fail_closed(tmp_path):
    _write(tmp_path / "alpaca-investment" / "account-status.json", {"application_status": "active"})

    message = build_investment_status(tmp_path)

    assert "有効です" in message
    assert "審査中" not in message


def test_prefers_live_snapshot_and_reports_every_wake_cadence(tmp_path):
    _write(tmp_path / "alpaca-investment" / "account-status.json", {"application_status": "active"})
    live = tmp_path / "alpaca-investment-live"
    _write(live / "observation-latest.json", {
        "mode": "live", "account": {"equity": "66.75", "cash": "0"}, "positions": [{}]})
    _write(live / "allocation-latest.json", {"approved": False, "reason": "risk gate"})
    _write(live / "risk-latest.json", {"official_pnl_ny_day_usd": "-0.02"})

    message = build_investment_status(tmp_path)

    assert "運転: live" in message
    assert "資産 $66.75" in message
    assert "日次純損益: -0.02" in message
    assert "全wakeをTelegramで報告" in message


def test_cli_prints_clickable_signup_url_for_new_user(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "investment_status.py"), "--state-root", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Alpacaで口座開設する: https://app.alpaca.markets/signup" in result.stdout


def test_running_bot_registers_invest_command():
    source = (SCRIPTS / "telegram_bot.py").read_text(encoding="utf-8")

    assert 'CommandHandler("invest", cmd_invest)' in source
    assert '"  /invest   show investment setup and loop status' in source
    assert "build_investment_reply" in source
