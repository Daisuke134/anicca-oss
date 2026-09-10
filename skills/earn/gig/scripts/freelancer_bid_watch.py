#!/usr/bin/env python3
"""Watch the Freelancer bids nobody was watching.

Four real bids were placed on 2026-08-02 from account Rasi4op (bidder 94117802) and then
left unobserved. On 2026-08-07 all four were still active, which means an award would have
arrived with nobody to notice it. This reads the public project endpoint, records a row per
check, and reports only when something changes.

Read-only by construction: it hits one public GET per project and never authenticates,
never bids, never messages. It does not touch the Coconala runtime, its browser, its lock
or its state.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://www.freelancer.com/api/projects/0.1/projects/{project_id}/?compact=true"
STATE = pathlib.Path.home() / "gig/freelancer/bid-watch.jsonl"
SHARED_SCRIPTS = pathlib.Path(__file__).resolve().parents[3] / "_shared/marketplace-core/scripts"
if str(SHARED_SCRIPTS) not in sys.path:
    sys.path.append(str(SHARED_SCRIPTS))
from telegram_delivery import send_via_shared_client  # noqa: E402

# bid id -> project id, from the 2026-08-02 live run recorded in
# docs/loop-engineering/27-gig-multi-marketplace-adapter-design.md
BIDS = {
    "491448418": "40620700",
    "491448768": "40620877",
    "491448805": "40620839",
    "491448885": "40620523",
}


def fetch(project_id: str, timeout: float) -> dict:
    request = urllib.request.Request(
        API.format(project_id=project_id), headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    result = payload.get("result") or {}
    currency = result.get("currency") or {}
    budget = result.get("budget") or {}
    stats = result.get("bid_stats") or {}
    return {
        "project_id": project_id,
        "status": result.get("status"),
        "sub_status": result.get("sub_status"),
        "frontend_status": result.get("frontend_project_status"),
        "title": (result.get("title") or "")[:80],
        "currency": currency.get("code"),
        "budget_min": budget.get("minimum"),
        "budget_max": budget.get("maximum"),
        "bid_count": stats.get("bid_count"),
        "time_submitted": result.get("time_submitted"),
    }


def last_seen() -> dict[str, dict]:
    if not STATE.is_file():
        return {}
    seen: dict[str, dict] = {}
    for line in STATE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("project_id"):
            seen[row["project_id"]] = row
    return seen


def notify(text: str, target: str) -> bool:
    """Diagnostic only. This channel never carries anything a buyer sees."""
    return bool(send_via_shared_client(text, chat_id=target).provider_id)


WATCHED = ("status", "sub_status", "frontend_status")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=os.environ.get("GIG_REPORT_CHAT", ""))
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--quiet", action="store_true", help="record but never notify")
    args = parser.parse_args()

    STATE.parent.mkdir(parents=True, exist_ok=True)
    previous = last_seen()
    now = datetime.now(timezone.utc).isoformat()
    changes: list[str] = []
    errors: list[str] = []
    rows: list[dict] = []

    for bid_id, project_id in BIDS.items():
        try:
            row = fetch(project_id, args.timeout)
        except (urllib.error.URLError, ValueError, TimeoutError) as error:
            # An unreachable endpoint is not "nothing changed" -- record it as its own fact.
            row = {"project_id": project_id, "error": type(error).__name__}
            errors.append(f"{project_id}: {type(error).__name__}")
        row.update({"bid_id": bid_id, "checked_at": now})
        rows.append(row)

        before = previous.get(project_id) or {}
        if before and "error" not in row:
            moved = [k for k in WATCHED if before.get(k) != row.get(k)]
            if moved:
                detail = ", ".join(f"{k}: {before.get(k)} -> {row.get(k)}" for k in moved)
                changes.append(f"bid {bid_id} / project {project_id}\n  {detail}\n  {row.get('title')}")

    with STATE.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    live = sum(1 for r in rows if r.get("status") == "active")
    print(json.dumps({
        "checked": len(rows), "active": live, "changed": len(changes),
        "errors": errors, "state": str(STATE),
    }, ensure_ascii=False))

    if changes and not args.quiet:
        body = ("Claude::: Freelancer の入札に動きがありました。\n\n"
                + "\n\n".join(changes)
                + "\n\nアカウント Rasi4op（bidder 94117802）。"
                  "落札であれば、返信・納品・入金の経路が未整備なので手当てが要ります。")
        notify(body, args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
