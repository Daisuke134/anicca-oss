#!/usr/bin/env python3
"""weekly_report.py — REQ-LV-111 wiring: for one of the current EDD loops (clip/affiliate/gig/
bounty), split its own metrics ledger into "this week"/"last week" (Mon-Sun, JST), score each half
through the loop's own evaluator.py::evaluate_stage1, decide beats_previous_week via
lib/weekly_compare.py, and append {ts, week_start, combined_score, beats_previous_week} to the same
ledger (REQ-LV-111's own exact instruction). Deciding which loop to run this on, and what to DO with
a losing week, stays with the agent/verify-loops-audit.sh caller -- this script only measures and
records, once per call. Weekly date-bucketing is a deterministic filter, not judgment.

CLI: python3 weekly_report.py <loop> [--ledger-path PATH] [--today YYYY-MM-DD]
"""
import argparse
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import zoneinfo
from pathlib import Path

SELF_IMPROVE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SELF_IMPROVE_DIR, "lib"))
from weekly_compare import beats_previous_week  # noqa: E402

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[3]))

LEDGER_PATH_FOR_LOOP = {
    "clip": lambda: os.environ.get("EARN_LEDGER") or os.path.expanduser("~/.local/state/life-manager/state/clip-earn-ledger.jsonl"),
    "affiliate": lambda: os.environ.get("AFFILIATE_METRICS_PATH") or os.path.expanduser("~/.cloak/affiliate-metrics.jsonl"),
    "gig": lambda: os.environ.get("GIG_FUNNEL_PATH") or os.path.expanduser("~/gig/gig-funnel.jsonl"),
    "bounty": lambda: os.environ.get("BOUNTY_FUNNEL_PATH") or str(
        REPO_ROOT / "skills/human-funded/bounty/state/bounty-funnel.jsonl"),
}


def _week_start(d: "datetime.date") -> "datetime.date":
    return d - datetime.timedelta(days=d.weekday())  # Monday, JST calendar


def _read_jsonl(path):
    rows = []
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _rows_in_week(rows, week_start):
    week_end = week_start + datetime.timedelta(days=7)
    out = []
    for r in rows:
        ts = r.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        d = datetime.datetime.fromtimestamp(ts, tz=JST).date()
        if week_start <= d < week_end:
            out.append(r)
    return out


def _load_evaluator(loop):
    spec = importlib.util.spec_from_file_location(f"{loop}_evaluator", os.path.join(SELF_IMPROVE_DIR, loop, "evaluator.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _score_for_rows(evaluator_mod, rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    try:
        with os.fdopen(fd, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return evaluator_mod.evaluate_stage1(path)["combined_score"]
    finally:
        os.unlink(path)


def _weekly_output_path(ledger_path):
    """REQ-LV-111 (F-ITER3-2 fix): the weekly comparison record MUST NOT land in the same file
    cadence-evidence.py's row-exists kind reads (it only checks "does a row with today's ts
    exist", so a weekly-report row landing there on a day with zero real activity would read as a
    false "cadence met today" — reproduced live by iteration-3 adversary review). Derives a
    sibling file (e.g. clip-earn-ledger.jsonl -> clip-earn-ledger-weekly.jsonl) next to the real
    metrics ledger, never the ledger itself."""
    base, ext = os.path.splitext(ledger_path)
    return f"{base}-weekly{ext or '.jsonl'}"


def run(loop, ledger_path=None, today=None, output_path=None):
    ledger_path = ledger_path or LEDGER_PATH_FOR_LOOP[loop]()
    output_path = output_path or _weekly_output_path(ledger_path)
    today_date = datetime.date.fromisoformat(today) if today else datetime.datetime.now(tz=JST).date()
    this_week_start = _week_start(today_date)
    last_week_start = this_week_start - datetime.timedelta(days=7)

    rows = _read_jsonl(ledger_path)
    evaluator_mod = _load_evaluator(loop)
    this_score = _score_for_rows(evaluator_mod, _rows_in_week(rows, this_week_start))
    last_score = _score_for_rows(evaluator_mod, _rows_in_week(rows, last_week_start))
    beats = beats_previous_week(this_score, last_score)

    record = {
        "ts": int(datetime.datetime.now(tz=JST).timestamp()),
        "week_start": this_week_start.isoformat(),
        "combined_score": this_score,
        "beats_previous_week": beats,
    }
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("loop", choices=list(LEDGER_PATH_FOR_LOOP.keys()))
    ap.add_argument("--ledger-path", default=None)
    ap.add_argument("--output-path", default=None)
    ap.add_argument("--today", default=None)
    a = ap.parse_args()
    print(json.dumps(run(a.loop, a.ledger_path, a.today, a.output_path), ensure_ascii=False))
