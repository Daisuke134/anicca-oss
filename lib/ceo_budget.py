#!/usr/bin/env python3
"""ceo_budget.py — REQ-CEO-013 budget computation + hard/soft breach write-gate. Imported by
bin/budget_check_cli.py (the bin/budget-check.sh backend) and by lib/registry-enforce.sh's dispatch
step (via bin/budget-check.sh). Pure computation + the shared atomic write-gate from
lib/registry_write_gate.py -- no agent judgment.
"""
import datetime
import json
import os
import time

try:
    from .registry_write_gate import append_jsonl
    from .ceo_allocation import effective_registry, write_allocation
except ImportError:  # Executed with lib/ directly on sys.path by repository CLIs.
    from registry_write_gate import append_jsonl
    from ceo_allocation import effective_registry, write_allocation

# REQ-CEO-001's 9 canonical loop keys -- used ONLY as a last-resort fallback when
# config/loop-registry.json itself is absent/corrupt (never as a judgment call, just the
# spec-fixed default loop set).
CANONICAL_LOOPS = [
    "bounty", "affiliate", "gig", "life-manager", "explorer",
    "capafy", "article", "pm", "hl",
]


def _safe_read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _period_keys(ts_epoch):
    dt = datetime.datetime.utcfromtimestamp(float(ts_epoch))
    month_key = dt.strftime("%Y-%m")
    iso_year, iso_week, _ = dt.isocalendar()
    week_key = f"{iso_year}-W{iso_week:02d}"
    return month_key, week_key


def _sum_costs(cost_events_path, loop, now_ts):
    monthly = 0.0
    weekly = 0.0
    if not os.path.isfile(cost_events_path):
        return monthly, weekly
    cur_month, cur_week = _period_keys(now_ts)
    with open(cost_events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict) or row.get("loop") != loop:
                continue
            usd = row.get("usd_estimate")
            ts = row.get("ts")
            if not isinstance(usd, (int, float)) or isinstance(usd, bool):
                continue
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            try:
                m, w = _period_keys(ts)
            except Exception:
                continue
            if m == cur_month:
                monthly += usd
            if w == cur_week:
                weekly += usd
    return monthly, weekly


def check_loop(base, loop, now_ts=None):
    """Computes period spend for `loop` against config/ceo-budget-config.json and, on a hard
    breach, atomically pauses the loop in the runtime allocation overlay + appends ONE
    ceo-decisions.jsonl row (idempotent -- a loop already paused is left untouched). On a soft
    breach, appends ONE lessons.jsonl warning row, no registry mutation. Returns a result dict;
    never raises for missing/malformed input (NFR-002)."""
    now_ts = now_ts if now_ts is not None else time.time()
    config_root = os.environ.get("CEO_CONFIG_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    budget_path = os.path.join(config_root, "config", "ceo-budget-config.json")
    ledgers_dir = os.path.join(base, "ledgers")
    cost_events_path = os.path.join(ledgers_dir, "cost-events.jsonl")
    lessons_path = os.path.join(ledgers_dir, "lessons.jsonl")
    decisions_path = os.path.join(ledgers_dir, "ceo-decisions.jsonl")

    budget_cfg = _safe_read_json(budget_path)
    if not isinstance(budget_cfg, dict) or loop not in budget_cfg:
        return {"loop": loop, "configured": False}

    limits = budget_cfg.get(loop) or {}
    monthly_limits = limits.get("monthly_usd") or {}
    weekly_limits = limits.get("weekly_usd") or {}
    monthly_sum, weekly_sum = _sum_costs(cost_events_path, loop, now_ts)

    result = {
        "loop": loop, "configured": True,
        "monthly_sum": monthly_sum, "weekly_sum": weekly_sum,
        "monthly_soft": monthly_limits.get("soft"), "monthly_hard": monthly_limits.get("hard"),
        "weekly_soft": weekly_limits.get("soft"), "weekly_hard": weekly_limits.get("hard"),
        "breach": None,
    }

    hard_breach = None
    if isinstance(monthly_limits.get("hard"), (int, float)) and monthly_sum >= monthly_limits["hard"]:
        hard_breach = ("monthly", monthly_sum, monthly_limits["hard"])
    elif isinstance(weekly_limits.get("hard"), (int, float)) and weekly_sum >= weekly_limits["hard"]:
        hard_breach = ("weekly", weekly_sum, weekly_limits["hard"])

    if hard_breach:
        period, sum_usd, hard_limit = hard_breach
        result["breach"] = "hard"
        unit_cfg = _safe_read_json(os.path.join(config_root, "config", "ceo-unit-economics.json"))
        if isinstance(unit_cfg, dict) and unit_cfg.get("mode") != "active":
            result["paused_now"] = False
            result["enforcement"] = "observe_only"
            return result
        try:
            registry = effective_registry(config_root, base)
        except Exception:
            registry = None
        already_paused = False
        if isinstance(registry, dict):
            loop_entry = registry.get("loops", {}).get(loop)
            if isinstance(loop_entry, dict):
                already_paused = loop_entry.get("allocation", {}).get("status") == "paused"
        if isinstance(registry, dict) and loop in registry.get("loops", {}) and not already_paused:
            allocation = dict(registry["loops"][loop].get("allocation") or {})
            allocation["status"] = "paused"
            write_allocation(base, loop, allocation)
            append_jsonl(decisions_path, {
                "ts": int(now_ts), "type": "hard-budget-breach", "loop": loop,
                "reason": "hard_budget_breach", "period": period, "sum_usd": sum_usd,
                "hard_limit": hard_limit,
            })
            result["paused_now"] = True
        else:
            result["paused_now"] = False
        return result

    soft_breach = None
    if isinstance(monthly_limits.get("soft"), (int, float)) and monthly_sum >= monthly_limits["soft"]:
        soft_breach = ("monthly", monthly_sum, monthly_limits["soft"])
    elif isinstance(weekly_limits.get("soft"), (int, float)) and weekly_sum >= weekly_limits["soft"]:
        soft_breach = ("weekly", weekly_sum, weekly_limits["soft"])
    if soft_breach:
        period, sum_usd, soft_limit = soft_breach
        append_jsonl(lessons_path, {
            "ts": int(now_ts), "loop": loop, "reason": "soft_budget_warning",
            "period": period, "sum_usd": sum_usd, "soft_limit": soft_limit,
        })
        result["breach"] = "soft"
    return result
