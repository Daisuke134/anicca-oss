#!/usr/bin/env python3
"""Tell Dais when the fleet runs out of fuel, once, while it still matters.

On 2026-08-04 one ChatGPT account hit its usage limit and the loops that depend
on it went quiet for eight hours. Nothing was broken in a way `launchctl list`
could see: the jobs started, ran and exited. The runner did notice — it wrote
error_class=transient_quota into its telemetry on every attempt — but that file
is read by nobody, so the signal existed and never arrived.

This turns that existing signal into one message. It invents no detection of its
own: if the runner did not record it, this says nothing.

    fuel-watch.py            look at the ledger, send if something is newly wrong
    fuel-watch.py --dry-run  print what would be sent, send nothing
    fuel-watch.py --status   current state, no side effects
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skills._shared.telegram import TelegramClient

# Overridable so the alert path can be exercised against a real slice of history
# rather than only against a quiet present. A watcher that has never been seen
# to fire is indistinguishable from one that cannot.
LEDGER = Path(os.environ.get("FUEL_WATCH_LEDGER",
                             Path.home() / ".local/state/anicca/telemetry/agent-usage.jsonl"))
STATE = Path(os.environ.get("FUEL_WATCH_STATE",
                            Path.home() / ".local/state/anicca/fuel-watch-state.json"))
JST = timezone(timedelta(hours=9))

# Only classes that mean "this provider cannot answer right now". A task that
# failed on its own merits is not a fuel problem and must not page anyone.
FUEL_FAILURES = {"transient_quota", "transient_auth"}

# How far back a failure still counts as current. Long enough to survive a quiet
# hour on an hourly loop, short enough that yesterday's outage does not page.
WINDOW = timedelta(hours=3)

# Say it again after this long if it is still broken — the same reason the
# telegram outbox re-sends a suppressed body after a day. Silence must never be
# the thing that tells you it is over.
REPEAT_AFTER = timedelta(hours=12)


def read_recent(now: datetime) -> list[dict]:
    """Failures inside the window. Reads the tail; the ledger only grows."""
    if not LEDGER.is_file():
        return []
    cutoff = (now - WINDOW).isoformat()
    rows = []
    with LEDGER.open(errors="replace") as handle:
        # 4000 lines is several days of a fleet this size, and bounds the read
        # so a watcher on a five-minute timer never walks a 2 MB file end to end.
        for line in handle.readlines()[-4000:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("error_class") in FUEL_FAILURES and row.get("timestamp", "") >= cutoff:
                rows.append(row)
    return rows


def summarise(rows: list[dict]) -> dict[str, dict]:
    """One entry per provider+account: how many, since when, which loops."""
    grouped: dict[str, dict] = {}
    for row in rows:
        key = f"{row.get('provider')}/{row.get('account') or 'default'}"
        entry = grouped.setdefault(key, {
            "count": 0, "first": row.get("timestamp", ""), "last": "",
            "loops": Counter(), "error_class": row.get("error_class"),
        })
        entry["count"] += 1
        stamp = row.get("timestamp", "")
        entry["first"] = min(entry["first"], stamp) if entry["first"] else stamp
        entry["last"] = max(entry["last"], stamp)
        entry["loops"][row.get("loop") or "?"] += 1
    return grouped


def jst(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone(JST).strftime("%-m月%-d日 %H:%M")
    except ValueError:
        return stamp or "不明"


def compose(key: str, entry: dict) -> str:
    loops = "、".join(f"{name}×{n}" for name, n in entry["loops"].most_common(4))
    reason = ("枠切れ（usage limit）" if entry["error_class"] == "transient_quota"
              else "認証エラー")
    return "\n".join((
        f"🚨 燃料が尽きています — {key}",
        "",
        f"理由: {reason}",
        f"最初の失敗: {jst(entry['first'])}",
        f"直近の失敗: {jst(entry['last'])}（{entry['count']}回）",
        f"影響を受けたループ: {loops}",
        "",
        "いま起きていること: 候補列の次のプロバイダへ自動で降りています。",
        "動きは止まりませんが、品質と課金は変わります。",
        "",
        "切り替えるなら: ~/.config/ai/bin/fleet-model.sh で現状を確認",
    ))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def send(message: str) -> str:
    result = TelegramClient.from_env().send_text(message)
    message_ids = result.get("message_ids") or []
    message_id = message_ids[-1] if message_ids else None
    if not message_id:
        raise RuntimeError("telegram ACK carried no message id")
    return str(message_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    grouped = summarise(read_recent(now))
    state = load_state()

    if args.status:
        if not grouped:
            print(f"  燃料の失敗: 直近{int(WINDOW.total_seconds() // 3600)}時間で0件")
        for key, entry in grouped.items():
            print(f"  {key}: {entry['count']}回 / 最初 {jst(entry['first'])} / "
                  f"loops={dict(entry['loops'])}")
        print(f"  通知済み: {list(state.get('sent', {}))}")
        return 0

    sent = state.setdefault("sent", {})
    for key, entry in grouped.items():
        previous = sent.get(key)
        if previous:
            try:
                if now - datetime.fromisoformat(previous["at"]) < REPEAT_AFTER:
                    continue
            except (ValueError, KeyError, TypeError):
                pass
        message = compose(key, entry)
        if args.dry_run:
            print(f"--- 送る内容（{key}）---\n{message}")
            continue
        message_id = send(message)
        sent[key] = {"at": now.isoformat(), "message_id": message_id, "count": entry["count"]}
        print(f"  送信: {key} messageId={message_id}")

    # A provider that has stopped failing is allowed to page again next time.
    for key in list(sent):
        if key not in grouped:
            del sent[key]

    if not args.dry_run:
        save_state(state)
    if not grouped:
        print("  燃料の失敗なし。何も送らない。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
