#!/usr/bin/env python3
"""score.py — decide which hooks won and which are dead, from measured posts.

Reward order (deepest available signal wins): revenue > paid > trial > install >
click > engagement > views. Today only engagement and views are measured per post, so
the score stops there and says so, rather than pretending a view is a customer.

Composite follows the one public implementation that closes this loop
(Upload-Post/skill-autoshorts): 0.6·views_normalised + 0.4·engagement_rate, top 20%
are winners. Two guards it does not have:
  * a post younger than --min-age-hours is not judged (early numbers are noise),
  * a cohort smaller than --min-cohort produces no winners at all, because ranking
    three posts teaches nothing.

Reads   post-metrics.jsonl (+ account-history.jsonl for the hook text)
Writes  brain.json — winners, killed, and the evidence behind each verdict.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

LIB = pathlib.Path(os.path.expanduser(os.environ.get(
    "MKT_LIBRARY_DIR", "~/.local/state/life-manager/marketing-engine/content-library")))
POST_METRICS = LIB / "post-metrics.jsonl"
HISTORY = LIB / "account-history.jsonl"
BRAIN = pathlib.Path(os.path.expanduser(os.environ.get(
    "MKT_BRAIN", "~/.local/state/life-manager/marketing-engine/content-library/brain.json")))


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


def latest_per_post(metrics: list[dict]) -> dict[str, dict]:
    """Keep the most recent reading of each post."""
    best: dict[str, dict] = {}
    for r in metrics:
        pid = r.get("post_id")
        if not pid:
            continue
        if pid not in best or (r.get("ts") or "") >= (best[pid].get("ts") or ""):
            best[pid] = r
    return best


def hooks_by_post(history: list[dict]) -> dict[str, str]:
    return {h["post_id"]: h.get("hook", "")
            for h in history if h.get("post_id") and h.get("hook")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-age-hours", type=float, default=24.0)
    ap.add_argument("--min-cohort", type=int, default=10)
    ap.add_argument("--kill-below-views", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    metrics = latest_per_post(rows(POST_METRICS))
    hooks = hooks_by_post(rows(HISTORY))

    judged = [r for r in metrics.values()
              if (r.get("age_hours") or 0) >= a.min_age_hours and r.get("views") is not None]
    print(f"posts measured={len(metrics)} judged(age>={a.min_age_hours}h)={len(judged)}")

    if len(judged) < a.min_cohort:
        print(f"NOT ENOUGH DATA: {len(judged)} judged posts < min cohort {a.min_cohort}. "
              f"No winner or kill is emitted — ranking a tiny cohort is superstition.")
        payload = {
            "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "insufficient_data",
            "judged": len(judged),
            "min_cohort": a.min_cohort,
            "winners": [],
            "killed": [],
        }
        if not a.dry_run:
            BRAIN.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    top_views = max((r["views"] or 0) for r in judged) or 1
    scored = []
    for r in judged:
        views = r.get("views") or 0
        engagement = ((r.get("likes") or 0) + (r.get("comments") or 0)
                      + (r.get("shares") or 0))
        rate = engagement / views if views else 0.0
        scored.append({
            "post_id": r["post_id"],
            "account": r.get("account"),
            "platform": r.get("platform"),
            "url": r.get("url"),
            "age_hours": r.get("age_hours"),
            "views": views,
            "engagement_rate": round(rate, 4),
            "score": round(0.6 * (views / top_views) + 0.4 * rate, 4),
            "hook": hooks.get(r["post_id"], ""),
        })

    scored.sort(key=lambda s: -s["score"])
    cut = max(1, len(scored) // 5)
    winners = scored[:cut]
    killed = [s for s in scored if s["views"] < a.kill_below_views]

    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "scored",
        "judged": len(judged),
        "reward_depth": "engagement",  # deepen to install/paid once attribution reports
        "winners": winners,
        "killed": killed,
    }
    if not a.dry_run:
        BRAIN.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    print(f"WINNERS (top {cut}):")
    for w in winners:
        print(f"  {w['views']:>7} views  rate={w['engagement_rate']:.3f}  "
              f"{w['account']}  {w['hook'][:44]!r}")
    print(f"KILLED (<{a.kill_below_views} views): {len(killed)}")
    print(f"-> {BRAIN}" if not a.dry_run else "-> dry run, nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
