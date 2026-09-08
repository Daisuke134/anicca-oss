#!/usr/bin/env python3
"""Catch the loop that runs perfectly and earns nothing.

Four layers can each be green or red independently, and only the last one is
money:

    liveness   the scheduler fired and the process exited
    fuel       the provider could answer
    coverage   every lane was actually checked
    effect     an application, a delivery, a contract, a payment

On 2026-08-04 the first three were green for eight hours while the fourth was
zero, and every check in place watched one of the first three. This watches the
fourth, and only the fourth.

The distinction it draws is between activity and productivity. A loop writing
incident and recovery events all night is busy, not productive — those are the
loop talking about itself. Productivity is an event with a counterparty.

    effect-watch.py            alert if a loop has been busy and unproductive
    effect-watch.py --status   show the counts, send nothing
    effect-watch.py --dry-run  print what would be sent
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skills._shared.telegram import TelegramClient

JST = timezone(timedelta(hours=9))
STATE = Path.home() / ".local" / "state" / "anicca" / "effect-watch-state.json"

# Events with a counterparty. Everything else is the loop describing itself.
PRODUCTIVE = {"application", "delivery", "contract", "payment", "reply_verified",
              "retainer_followthrough", "application_recovery"}

WATCHED = {
    "gig": {
        "events": Path.home() / "gig" / "work-events.jsonl",
        "time_field": "occurred_at",
        "kind_field": "kind",
        "quiet_hours": 24,
    },
}

# 抑制窓は実行間隔より短くする。このジョブは毎日 12:00 に 1 回だけ走るので、窓を 24 時間に
# すると、ある日の送信が 12:00:05 になった翌日の実行 (12:00:03) は経過 23:59:58 で窓の内側に
# 落ち、黙って飛ばされる。以後は 1 日おきにしか鳴らない。20 時間なら日次実行は必ず通り、
# 同じ日の重複 (手動実行や kickstart) は従来どおり抑えられる。
REPEAT_AFTER = timedelta(hours=20)


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def assess(name: str, config: dict, now: datetime) -> dict | None:
    rows = _load(config["events"])
    if not rows:
        return None
    cutoff = (now - timedelta(hours=config["quiet_hours"])).isoformat()
    recent = [r for r in rows if str(r.get(config["time_field"], "")) >= cutoff]
    if not recent:
        # Nothing at all in the window is a liveness problem, not an effect one;
        # fleet-daily already reports last activity. Saying it here as well would
        # be two alerts for one fact.
        return None
    kinds = Counter(r.get(config["kind_field"]) for r in recent)
    productive = sum(count for kind, count in kinds.items() if kind in PRODUCTIVE)
    if productive:
        return None
    last_productive = ""
    for row in reversed(rows):
        if row.get(config["kind_field"]) in PRODUCTIVE:
            last_productive = str(row.get(config["time_field"], ""))
            break
    return {
        "loop": name,
        "window_hours": config["quiet_hours"],
        "activity": sum(kinds.values()),
        "kinds": kinds,
        "last_productive": last_productive,
    }


def jst(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone(JST).strftime("%-m月%-d日 %H:%M")
    except ValueError:
        return stamp or "記録なし"


def compose(finding: dict) -> str:
    busy = "、".join(f"{kind}×{count}" for kind, count in finding["kinds"].most_common(4))
    since = jst(finding["last_productive"])
    return "\n".join((
        f"🟡 {finding['loop']} は動いているが成果がありません",
        "",
        f"直近{finding['window_hours']}時間: イベント {finding['activity']}件、"
        f"うち相手のいる成果 0件",
        f"内訳: {busy}",
        f"最後に成果が出たのは: {since}",
        "",
        "稼働も燃料も緑でこうなることがあります。",
        "プロセスが動いていることと、金が動いていることは別です。",
    ))


def send(message: str) -> str:
    payload = TelegramClient.from_env().send_text(message)
    message_ids = payload.get("message_ids") or []
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
    findings = [f for f in (assess(n, c, now) for n, c in WATCHED.items()) if f]

    if args.status:
        for name, config in WATCHED.items():
            finding = assess(name, config, now)
            if finding:
                print(f"  {name}: 活動 {finding['activity']}件 / 成果 0件 / "
                      f"最後の成果 {jst(finding['last_productive'])}")
            else:
                print(f"  {name}: 成果あり、または窓内に活動なし")
        return 0

    try:
        state = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    sent = state.setdefault("sent", {})

    for finding in findings:
        previous = sent.get(finding["loop"])
        if previous:
            try:
                if now - datetime.fromisoformat(previous["at"]) < REPEAT_AFTER:
                    continue
            except (ValueError, KeyError, TypeError):
                pass
        message = compose(finding)
        if args.dry_run:
            print(f"--- 送る内容（{finding['loop']}）---\n{message}")
            continue
        message_id = send(message)
        sent[finding["loop"]] = {"at": now.isoformat(), "message_id": message_id}
        print(f"  送信: {finding['loop']} messageId={message_id}")

    for name in list(sent):
        if name not in {f["loop"] for f in findings}:
            del sent[name]

    if not args.dry_run:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    if not findings:
        print("  成果ゼロのループなし。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
