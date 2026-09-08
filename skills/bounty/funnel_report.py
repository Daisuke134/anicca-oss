#!/usr/bin/env python3
"""funnel_report.py — REQ-LV-016 wiring: reads state/gated.json + state/attempts.jsonl (real
run.sh-written files), summarizes via the frozen summarize_bounty_funnel, and appends ONE
{ts, pass_id, checked, survivors, claimed, submitted, stalled} row to state/bounty-funnel.jsonl.
This is the SAME path skills/self/cadence-evidence.py already reads via its BOUNTY_FUNNEL_PATH
override default (REQ-LV-100/104 cadence contract for bounty, increment kind on `checked`) — so
once this script runs at the end of a real pass, the bounty cadence contract has real evidence.

CLI: python3 funnel_report.py [--pass-id ID] [--gated-path PATH] [--attempts-path PATH] [--out-path PATH]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from funnel import summarize_bounty_funnel  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("BOUNTY_STATE_DIR", os.path.join(HERE, "state"))
DEFAULT_GATED = os.path.join(STATE_DIR, "gated.json")
DEFAULT_ATTEMPTS = os.path.join(STATE_DIR, "attempts.jsonl")
DEFAULT_OUT = os.environ.get(
    "BOUNTY_FUNNEL_REPORT",
    os.path.join(STATE_DIR, "bounty-funnel.jsonl"),
)


def _read_json(path):
    if not os.path.exists(path):
        return {}
    try:
        return json.load(open(path))
    except Exception:
        return {}


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


def run(gated_path=DEFAULT_GATED, attempts_path=DEFAULT_ATTEMPTS, out_path=DEFAULT_OUT, pass_id=None):
    gated = _read_json(gated_path)
    attempts = _read_jsonl(attempts_path)
    summary = summarize_bounty_funnel(gated, attempts)
    record = {"ts": int(time.time()), "pass_id": pass_id, **summary}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass-id", default=None)
    ap.add_argument("--gated-path", default=DEFAULT_GATED)
    ap.add_argument("--attempts-path", default=DEFAULT_ATTEMPTS)
    ap.add_argument("--out-path", default=DEFAULT_OUT)
    a = ap.parse_args()
    print(json.dumps(run(a.gated_path, a.attempts_path, a.out_path, a.pass_id), ensure_ascii=False))
