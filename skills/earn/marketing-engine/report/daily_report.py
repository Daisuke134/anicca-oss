#!/usr/bin/env python3
"""daily_report.py — one Telegram message a day, money first, no logs.

Reads only what was actually measured:
  daily-metrics.jsonl  money (RevenueCat, Stripe)
  post-metrics.jsonl   engagement per post
  brain.json           the day's verdicts, including "not enough data"
  account-history.jsonl hook text for the named posts

Rules this report obeys:
  * money leads; views are supporting evidence, never the headline
  * a missing number prints as "unavailable", never as 0
  * a day with no change prints "nothing was learned today" — silence is the bug
  * raw logs never appear; failures appear as a named danger line

  daily_report.py [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills._shared.telegram import TelegramClient, TelegramError

LIB = pathlib.Path(os.path.expanduser(os.environ.get(
    "MKT_LIBRARY_DIR", "~/.local/state/life-manager/marketing-engine/content-library")))
TELEGRAM_TARGET = os.environ.get("MKT_TELEGRAM_TARGET")


def rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line, strict=False))
        except json.JSONDecodeError:
            continue
    return out


def fmt(value, suffix: str = "") -> str:
    return "unavailable" if value is None else f"{value}{suffix}"


def money_section() -> list[str]:
    metrics = rows(LIB / "daily-metrics.jsonl")
    if not metrics:
        return ["money: unavailable (no metrics row yet)"]
    now, prev = metrics[-1], (metrics[-2] if len(metrics) > 1 else None)
    rc = now.get("revenuecat", {})
    st = now.get("stripe", {})

    line = f"MRR {fmt(rc.get('mrr'), ' USD')}"
    if prev:
        before = prev.get("revenuecat", {}).get("mrr")
        if isinstance(before, (int, float)) and isinstance(rc.get("mrr"), (int, float)):
            line += f" ({rc['mrr'] - before:+g} vs previous reading)"
    out = [line,
           f"subs {fmt(rc.get('active_subscriptions'))} · "
           f"trials {fmt(rc.get('active_trials'))} · "
           f"new customers 28d {fmt(rc.get('new_customers'))}"]
    gross = st.get("gross_24h") or {}
    if gross:
        out.append("one-off sales 24h: "
                   + ", ".join(f"{v/100:.2f} {k.upper()}" for k, v in gross.items()))
    else:
        out.append(f"one-off sales 24h: {fmt(st.get('succeeded_payments_24h'))} payments")
    return out


def posts_section() -> list[str]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    recent = []
    for r in rows(LIB / "post-metrics.jsonl"):
        try:
            ts = dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts >= cutoff and r.get("views") is not None:
            recent.append(r)
    if not recent:
        return ["posts measured in 24h: none"]

    latest: dict[str, dict] = {}
    for r in recent:
        latest[r["post_id"]] = r
    total_views = sum(r["views"] for r in latest.values())
    zero = [r for r in latest.values() if r["views"] == 0]
    out = [f"posts measured 24h: {len(latest)} · total views {total_views}"]
    if zero:
        out.append(f"zero-view posts: {len(zero)} (reach check needed)")
    return out


def verdict_section() -> list[str]:
    path = LIB / "brain.json"
    if not path.exists():
        return ["verdict: unavailable (scorer has not run)"]
    brain = json.loads(path.read_text())
    if brain.get("status") == "insufficient_data":
        return [f"verdict: none — only {brain.get('judged')} posts old enough to judge "
                f"(need {brain.get('min_cohort')}). Nothing was learned today."]
    winners = brain.get("winners", [])
    killed = brain.get("killed", [])
    out = [f"verdict: {len(winners)} winner(s), {len(killed)} killed"]
    for w in winners[:3]:
        hook = (w.get("hook") or "").replace("\n", " ")[:48]
        out.append(f"  win {w.get('views')} views · {w.get('account')} · {hook!r}")
    return out


def send(message: str) -> bool:
    try:
        receipt = TelegramClient.from_env().send_text(message, chat_id=TELEGRAM_TARGET)
    except TelegramError as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return False
    print(f"telegram_message_ids={receipt['message_ids']}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    body = [f"ANICCA daily · {today}", ""]
    body += money_section() + [""] + posts_section() + [""] + verdict_section()
    message = "\n".join(body)

    if a.dry_run:
        print(message)
        return 0
    ok = send(message)
    print("sent" if ok else "send failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
