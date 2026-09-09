"""Build the local /invest reply from the investment loop's existing receipts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ALPACA_SIGNUP_URL = "https://app.alpaca.markets/signup"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _money_or_unknown(value) -> str:
    if isinstance(value, bool) or value is None:
        return "不明"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "不明"
    return str(value) if number == number and abs(number) != float("inf") else "不明"


def build_investment_reply(state_root: Path) -> dict:
    root = state_root / "alpaca-investment"
    account_path = root / "account-status.json"
    account = _read_json(account_path)
    snapshot_root = next((candidate for candidate in (
        state_root / "alpaca-investment-live",
        state_root / "alpaca-investment-shadow",
        root,
    ) if (candidate / "observation-latest.json").is_file()), root)
    observation = _read_json(snapshot_root / "observation-latest.json")
    allocation = _read_json(snapshot_root / "allocation-latest.json")
    risk = _read_json(snapshot_root / "risk-latest.json")

    if not account_path.exists():
        return {
            "text": "\n".join([
                "Investment Loop",
                "",
                "Alpacaで口座開設と本人確認を完了してください。",
                "Life Managerと同じメールアドレスを使うと接続が簡単です。",
                "完了後はLife Managerが審査状態を確認し、自動運転まで進めます。",
            ]),
            "reply_markup": {"inline_keyboard": [[
                {"text": "Alpacaで口座開設する", "url": ALPACA_SIGNUP_URL},
                {"text": "今はしない", "callback_data": "invest:later"},
            ]]},
        }

    application_status = str(account.get("application_status") or "unknown").lower()
    paper_account = observation.get("account") if isinstance(observation.get("account"), dict) else {}
    equity = paper_account.get("equity")
    cash = paper_account.get("cash")
    reason = allocation.get("reason")
    decision = "取引なし" if allocation.get("approved") is False else "取引候補あり"

    lines = ["Investment Loop", ""]
    if application_status == "in_review":
        lines.append("ライブ口座: 審査中です。今は操作不要です。承認を確認したら、次に必要な操作だけ知らせます。")
    elif application_status in {"approved", "active"}:
        lines.append("ライブ口座: 有効です。現在の自動運転状態を下に表示します。")
    elif application_status in {"rejected", "action_required"}:
        lines.append("ライブ口座: 追加対応が必要です。Alpacaの画面で表示される本人対応だけ行ってください。")
    else:
        lines.append("ライブ口座: 状態をまだ確認できません。ライブ注文は出しません。")

    if equity is not None and cash is not None:
        mode = observation.get("mode") if observation.get("mode") in {"paper", "shadow", "live"} else "不明"
        lines.append(f"運転: {mode}。資産 ${equity}、現金 ${cash}、今回の判断は{decision}です。")
        if reason:
            lines.append(f"理由: {reason}")
        lines.append(f"日次純損益: {_money_or_unknown(risk.get('official_pnl_ny_day_usd'))}")
        lines.append("次回確認: 5分後（全wakeをTelegramで報告）")
    else:
        lines.append("運転状態: 最新snapshotをまだ読み取れません。5分後に自動再確認します。")
    return {"text": "\n".join(lines)}


def build_investment_status(state_root: Path) -> str:
    """Compatibility helper for callers that only need message text."""
    return build_investment_reply(state_root)["text"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".local" / "state" / "life-manager",
    )
    args = parser.parse_args()
    reply = build_investment_reply(args.state_root)
    print(reply["text"])
    keyboard = reply.get("reply_markup", {}).get("inline_keyboard", [])
    for row in keyboard:
        for button in row:
            if button.get("url"):
                print(f"\n{button['text']}: {button['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
