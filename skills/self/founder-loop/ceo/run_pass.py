#!/usr/bin/env python3
"""run_pass.py — REQ-CEO-058: one CEO pass. DAILY (light, read-only) unless `is_ceo_weekly_due()`
fires, in which case the full WEEKLY sequence (steps (1)-(12) below, numbered per REQ-CEO-058) runs.
Called by ceo-pass.sh; state root overridable via CEO_STATE_DIR (test seam, mirrors founder-loop.sh's
own FOUNDER_TEST/FOUNDER_DIR pattern) so tests never touch the real ~/.anicca-founder/state/.

Every write in this module goes through allocator.write_registry_atomic /
allocator/budget/bandit's own atomic-write helpers (tmp-write + os.replace) -- the same pattern
record-earn.mjs's writeCursorAtomic uses. loop-registry.json is written exactly once per WEEKLY pass,
at step (9) -- INV-CEO-2.

Step (8) (agent allocation decision) never lets an agent-proposed decision reach loop-registry.json
unchecked (Phase 3 iteration-1 adversary B-1): REQ-CEO-012 only forbids the *bandit* from picking the
winner and being auto-written -- it does NOT exempt the independent deterministic gates (budget
compliance REQ-CEO-022, capital-increase-within-realized-profit REQ-CEO-030(b), the fleet guardrail
triad REQ-CEO-031, the scale-down state machine REQ-CEO-032/034, and the escalation schema gate
REQ-CEO-060) from being applied to whatever the agent proposes. Every candidate decision (from
CEO_AGENT_DECISIONS_JSON, or none at all) is passed through all five gates below before it can appear
in `allocation_decisions`; the scale-down check itself runs for the whole roster every open pass
regardless of whether the agent supplied a decision file, since it is a protective/defensive action,
not a resource-increase request."""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import zoneinfo
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import allocator  # noqa: E402
import bandit  # noqa: E402
import budget  # noqa: E402
from budget_pacer import BudgetPacer  # noqa: E402

# Phase 5 hardening Finding F1: each roster loop's REAL earn ledger is read via the SAME existing
# modules weekly_report.py itself uses (LEDGER_PATH_FOR_LOOP's real production paths, env-overridable
# for tests -- EARN_LEDGER/AFFILIATE_METRICS_PATH/EARN_VIDEO_METRICS_PATH/GIG_FUNNEL_PATH/
# BOUNTY_FUNNEL_PATH) + ledger_metrics.load_ledger_rows -- never a fabricated per-loop convention
# inside this feature's own state dir (車輪の再発明禁止, `~/.claude/rules/
# building-effective-ai-agents.md`: import and call, do not re-derive).
SELF_IMPROVE_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "self-improve")
for _p in (SELF_IMPROVE_DIR, os.path.join(SELF_IMPROVE_DIR, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import weekly_report  # noqa: E402
from ledger_metrics import load_ledger_rows  # noqa: E402

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[4]))


def _state_dir() -> str:
    root = os.environ.get("LIFE_MANAGER_STATE_ROOT") or os.path.expanduser(
        "~/.local/state/life-manager/founder-loop-cadence"
    )
    return os.environ.get("CEO_STATE_DIR") or os.path.join(root, "state")


def _cadence_contracts_path() -> str:
    return os.environ.get("CEO_CADENCE_CONTRACTS") or str(
        REPO_ROOT / "skills/self/cadence-contracts.json"
    )


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def _write_json_atomic(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _append_jsonl_atomic(path: str, row: dict) -> None:
    existing = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(existing + json.dumps(row, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _today_jst() -> str:
    return datetime.datetime.now(JST).date().isoformat()


def _latest_per_loop_scores(rows: list) -> dict:
    """Last realized_profit_usd seen per loop in ceo-loop-score-history.jsonl (append-only, one row
    per loop per WEEKLY pass) -- used as "last week's score" for REQ-CEO-032's beats_previous_week
    input. Simple last-row-wins scan (file is small, one WEEKLY pass at a time)."""
    latest: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        loop = row.get("loop")
        if loop:
            latest[loop] = row.get("realized_profit_usd", 0.0)
    return latest


def _send_mail_best_effort(report_script: str, loop_or_ceo: str, summary: str, result: str, earned: str, evidence: str) -> None:
    """Best-effort mail send via the existing loop-report.sh interface -- failure here must never
    fail the WEEKLY pass (mirrors step (12)'s own try/except)."""
    try:
        subprocess.run(
            ["bash", report_script, loop_or_ceo, summary, result, earned, evidence],
            check=False, timeout=30,
        )
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    state_dir = _state_dir()
    os.makedirs(state_dir, exist_ok=True)
    marker_log = os.path.join(state_dir, "ceo-pass.log")
    today = _today_jst()
    ts_now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report_script = os.path.join(HERE, "..", "..", "..", "report", "loop-report.sh")

    # PROP-CEO-022: this marker MUST be written unconditionally, first, before anything below can
    # fail -- it is the direct evidence that ceo-pass.sh ran even on a RC!=0 founder-loop wake.
    with open(marker_log, "a", encoding="utf-8") as f:
        f.write(f"{ts_now} ceo-pass ran (today_jst={today})\n")

    weekly_meta_path = os.path.join(state_dir, "ceo-weekly-meta.json")
    weekly_meta = _read_json(weekly_meta_path, {"last_ceo_run_jst_date": None})
    is_weekly = allocator.is_ceo_weekly_due(weekly_meta.get("last_ceo_run_jst_date"), today)

    cadence_contract = _read_json(_cadence_contracts_path(), {})
    roster = allocator.derive_roster(cadence_contract) if cadence_contract else []

    if not is_weekly:
        # REQ-CEO-003: DAILY pass reads the same snapshot shape but never writes bandit state,
        # loop-registry.json, or ceo-verification.jsonl.
        print(json.dumps({"pass": "daily", "today": today, "roster": roster}))
        return 0

    # ---------------- WEEKLY pass, REQ-CEO-058 steps (1)-(12) ----------------
    fx_config = _read_json(os.path.join(state_dir, "ceo-fx-config.json"), {"jpy_usd_rate": 150.0})
    registry_path = os.path.join(state_dir, "loop-registry.json")
    existing_registry = allocator.bootstrap_registry_if_missing(registry_path)  # (1)

    budget_cfg = _read_json(os.path.join(state_dir, "ceo-budget-config.json"), None)
    cost_events_path = os.path.join(state_dir, "ceo-cost-events.jsonl")
    cost_rows = budget.load_cost_events(cost_events_path)
    month_key = ts_now[:7]
    monthly_spend = budget.monthly_spend_by_loop(cost_rows, month_key)
    weekly_spend = budget.weekly_spend_by_loop(cost_rows, today)  # (2)

    # (3-a/b): bandit + BudgetPacer update -- unconditional, every WEEKLY pass (PROP-CEO-023).
    bandit_state_path = os.path.join(state_dir, "ceo-bandit-state.json")
    arms = bandit.load_state(bandit_state_path)
    pacer_path = os.path.join(state_dir, "ceo-budget-pacer-state.json")
    pacer = BudgetPacer.load(pacer_path)
    pacer.update(sum(weekly_spend.values()))
    pacer.save(pacer_path)

    # (2, REQ-CEO-002(c)) each roster loop's REAL earn ledger, read via the SAME production paths
    # weekly_report.py itself resolves (LEDGER_PATH_FOR_LOOP: clip/affiliate/video/gig/bounty) --
    # Phase 5 hardening Finding F1 fix, replaces the previous fabricated
    # `{state_dir}/{loop}-earn-ledger.jsonl` convention that no loop CLI has ever written to.
    per_loop_entries = {}
    for loop in roster:
        ledger_resolver = weekly_report.LEDGER_PATH_FOR_LOOP.get(loop)
        if ledger_resolver is not None:
            rows = load_ledger_rows(ledger_resolver())
        else:
            # pm-earner (and any future roster loop not yet in
            # weekly_report.LEDGER_PATH_FOR_LOOP): no real earn-ledger source has been declared for
            # this loop yet. Honestly empty (mirrors REQ-CEO-021's B12 pm-earner spend-side fallback
            # -- absent data is never zero-filled with a fabricated non-zero) rather than inventing a
            # new per-loop file convention this feature would be the only reader AND writer of.
            rows = []
        per_loop_entries[loop] = allocator.sum_earn_by_currency(rows)

    for loop in roster:
        realized = allocator.realized_profit_usd(per_loop_entries.get(loop, []), fx_config)
        reward = bandit.compute_reward(realized, weekly_spend.get(loop, 0.0), pacer.lambda_)
        context = [1.0, weekly_spend.get(loop, 0.0), realized]
        arms = bandit.update_arm(arms, loop, context, reward, d=len(context), prior=0.5)
    bandit.save_state(bandit_state_path, arms)

    # (3-c): budget fail-open log + snapshot + soft-warn/hard-stop alert dedup (REQ-CEO-023/024/025)
    # -- unconditional, every WEEKLY pass, regardless of cooldown/rollback (PROP-CEO-023).
    alerts_path = os.path.join(state_dir, "ceo-budget-alerts.jsonl")
    fired_alert_keys = budget.load_fired_alert_keys(alerts_path)
    budget_snapshot_by_loop = {}
    for loop in roster:
        loop_budget = budget.budget_for_loop(budget_cfg, loop)
        if loop_budget is None:
            budget.warn_if_budgets_missing(marker_log, loop)
        spend = monthly_spend.get(loop, 0.0)
        budget_snapshot_by_loop[loop] = budget.budget_snapshot_for_registry(budget_cfg, loop, spend)
        if loop_budget is None:
            continue
        for threshold_name in ("soft_warn", "hard_stop"):
            threshold_val = loop_budget.get(f"{threshold_name}_usd")
            if threshold_val is None or spend < threshold_val:
                continue
            key = budget.alert_key(month_key, loop, threshold_name)
            if not budget.should_fire_alert(key, fired_alert_keys):
                continue
            budget.record_alert_fired(alerts_path, key)
            fired_alert_keys.add(key)
            _send_mail_best_effort(
                report_script, loop, f"budget {threshold_name} reached",
                "success", "0", f"none: spend_usd={spend} threshold={threshold_val}",
            )

    # (4): company_score
    this_week_score = allocator.company_score(per_loop_entries, fx_config)
    verification_path = os.path.join(state_dir, "ceo-verification.jsonl")
    prior_rows = [r for r in budget.load_cost_events(verification_path) if isinstance(r, dict)]
    prev_week_score = prior_rows[-1]["this_week_company_score"] if prior_rows else 0.0
    beats = this_week_score > prev_week_score

    score_history_path = os.path.join(state_dir, "ceo-loop-score-history.jsonl")
    prev_loop_scores = _latest_per_loop_scores(budget.load_cost_events(score_history_path))

    # (6): rollback state machine
    miss_streak_path = os.path.join(state_dir, "ceo-miss-streak.json")
    miss_streak = _read_json(
        miss_streak_path, {"consecutive_miss_count": 0, "cooldown_weeks_remaining": 0}
    )
    count_in = miss_streak["consecutive_miss_count"]
    cooldown_in = miss_streak["cooldown_weeks_remaining"]

    rollback_path = os.path.join(state_dir, "ceo-rollback.json")
    rollback_snapshot = _read_json(rollback_path, None)
    if allocator.should_snapshot(count_in, cooldown_in):
        rollback_snapshot = {
            loop: {"allocation": existing_registry[loop].get("allocation")}
            for loop in roster
            if loop in existing_registry
        }
        _write_json_atomic(rollback_path, rollback_snapshot)

    count_new = allocator.update_miss_count(count_in, beats, cooldown_in)
    rollback_fired = allocator.should_rollback(count_new, cooldown_in)
    rollback_restore = (
        allocator.restore_from_rollback(rollback_snapshot)
        if rollback_fired and rollback_snapshot
        else None
    )

    # (8): allocation-decision gate. Skipped entirely (allocation_decisions stays {}) when cooldown
    # is active or a rollback just fired this pass (the formal `gate_open` condition, REQ-CEO-058 B6).
    allocation_decisions = {}
    gate_open = (cooldown_in == 0) and (not rollback_fired)

    if gate_open:
        # Phase 5 hardening Finding F4: a MISSING ceo-allocation-ranges.json must fail CLOSED (this
        # feature's own shipped default ranges apply), never fail-open. A file that DOES exist on
        # disk still overrides these defaults (operator intent wins).
        ranges_cfg = _read_json(
            os.path.join(state_dir, "ceo-allocation-ranges.json"), None
        )
        if ranges_cfg is None:
            ranges_cfg = allocator.DEFAULT_ALLOCATION_RANGES
        fleet_cfg = _read_json(os.path.join(state_dir, "ceo-fleet-config.json"), {})
        lessons_path = os.path.join(state_dir, "ceo-lessons.jsonl")
        escalations_path = os.path.join(state_dir, "ceo-escalations.jsonl")
        disk_free_gb = shutil.disk_usage(state_dir).free / (1024 ** 3)
        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

        # (8-a) REQ-CEO-022: hard-stop-exceeded loops excluded from new resource increases (existing
        # allocation is left untouched for them -- hard-stop never stops the loop from running).
        budget_passed, _budget_skipped = budget.filter_budget_compliant_loops(
            list(roster), budget_cfg, monthly_spend
        )

        # (8-b) REQ-CEO-011: UCB scores surfaced for the agent -- display only, never auto-written
        # (REQ-CEO-012's judgment/tool boundary).
        ucb_scores = bandit.select_scores([1.0, 0.0, 0.0], arms, alpha=1.0)
        print(json.dumps({"step": "8-b_ucb_scores_for_agent", "scores": ucb_scores}))

        # Phase 5 hardening Finding F3: CEO_AGENT_DECISIONS_JSON is an EXTERNAL, agent-controlled
        # input -- a syntactically-valid-but-wrong-shape file (array/bare string/null/a per-loop
        # value that isn't itself a dict) must never crash the WEEKLY pass. Any shape violation is
        # treated as "no decision for that loop" and logged, not raised.
        candidate_decisions = {}
        decisions_path = os.environ.get("CEO_AGENT_DECISIONS_JSON")
        if decisions_path and os.path.exists(decisions_path):
            raw_decisions = _read_json(decisions_path, {})
            if isinstance(raw_decisions, dict):
                candidate_decisions = raw_decisions
            else:
                print(json.dumps({
                    "step": "8_agent_decisions_rejected",
                    "reason": "CEO_AGENT_DECISIONS_JSON top-level is not a dict",
                    "type": type(raw_decisions).__name__,
                }))

        for loop in roster:
            loop_realized_profit = allocator.realized_profit_usd(per_loop_entries.get(loop, []), fx_config)
            existing_loop_entry = existing_registry.get(loop, {})
            existing_allocation = dict(existing_loop_entry.get("allocation") or {})
            is_over_budget = loop not in budget_passed

            decision = candidate_decisions.get(loop)
            if not isinstance(decision, dict):
                if decision is not None:
                    print(json.dumps({
                        "step": "8_agent_decision_rejected", "loop": loop,
                        "reason": "per-loop decision value is not a dict", "type": type(decision).__name__,
                    }))
                decision = None
            proposed_allocation = decision.get("allocation", {}) if decision else {}
            allocation = dict(proposed_allocation) if isinstance(proposed_allocation, dict) else {}

            # REQ-CEO-042: reject the whole proposal if any field is out of its configured range.
            if decision and not allocator.validate_allocation_ranges(allocation, ranges_cfg):
                allocation = {}

            # (8-c/REQ-CEO-030(b)): capital_cap_usd increase clamped to the loop's own realized
            # profit -- INV-CEO-1, `loop_realized_profit` is already realized_profit_usd()-converted.
            # (8-a applied): budget hard-stop blocks ANY increase outright, regardless of profit.
            if "capital_cap_usd" in allocation:
                old_cap = existing_allocation.get("capital_cap_usd", 0)
                new_cap = allocation["capital_cap_usd"]
                if new_cap > old_cap:
                    if is_over_budget:
                        allocation["capital_cap_usd"] = old_cap
                    elif not allocator.capital_increase_within_realized_profit(
                        new_cap, old_cap, loop_realized_profit
                    ):
                        allocation["capital_cap_usd"] = min(
                            new_cap, old_cap + max(0.0, loop_realized_profit)
                        )

            # (8-c/REQ-CEO-031): fleet_size_target increase gated by the existing guardrail triad
            # (reused as-is, no re-derivation) AND budget compliance. `streak` intentionally
            # fail-closed-defaults to 0 (blocks the increase) when no independent streak source has
            # been wired for this loop yet -- never fail-open on an unknown streak.
            if "fleet_size_target" in allocation:
                old_fleet = existing_allocation.get("fleet_size_target", 1)
                new_fleet = allocation["fleet_size_target"]
                if new_fleet > old_fleet:
                    streak = existing_loop_entry.get("streak", 0)
                    allowed = (not is_over_budget) and allocator.fleet_increase_allowed(
                        streak=streak,
                        weekly_score=loop_realized_profit,
                        weekly_score_threshold=fleet_cfg.get("weekly_score_threshold", 0.0),
                        disk_free_gb=disk_free_gb,
                        last_spawn_ts=existing_loop_entry.get("fleet_last_spawn_ts"),
                        now_ts=now_ts,
                        min_interval_days=fleet_cfg.get("min_interval_days", 7),
                        current_count=old_fleet,
                        max_count=fleet_cfg.get("max_count", 5),
                    )
                    if not allowed:
                        allocation["fleet_size_target"] = old_fleet
                    else:
                        # (8-e/REQ-CEO-060): fleet-increase escalation, schema-gated (presence only,
                        # never content judgment), weekly_realized_profit_usd is the SAME
                        # already-converted value (INV-CEO-1) -- never the raw native-currency amount.
                        escalation = {
                            "ts": ts_now,
                            "loop": loop,
                            "escalation_type": "fleet_increase",
                            "justification": decision.get("justification", "") if decision else "",
                            "weekly_realized_profit_usd": loop_realized_profit,
                        }
                        if allocator.validate_escalation_schema(escalation):
                            _append_jsonl_atomic(escalations_path, escalation)

            # (8-c/REQ-CEO-032, 8-d/REQ-CEO-034): scale-down evaluation runs for EVERY roster loop
            # this open pass, independent of whether the agent proposed anything for it -- this is a
            # protective/defensive backstop, not a resource-increase request.
            prev_bad_weeks = existing_loop_entry.get("consecutive_bad_weeks", 0) or 0
            loop_beats = loop_realized_profit > prev_loop_scores.get(loop, 0.0)
            scale_action = allocator.should_scale_down(loop_realized_profit, loop_beats, prev_bad_weeks)
            new_bad_weeks = prev_bad_weeks + 1 if scale_action != "none" else 0
            if scale_action == "reduce_frequency":
                allocation["pass_frequency_multiplier"] = min(
                    allocation.get(
                        "pass_frequency_multiplier",
                        existing_allocation.get("pass_frequency_multiplier", 1.0),
                    ),
                    0.5,
                )
            elif scale_action == "pause":
                allocation["status"] = "paused"
                _append_jsonl_atomic(
                    lessons_path,
                    allocator.build_lesson_row(
                        ts=ts_now, loop=loop, action="paused",
                        reason=f"consecutive_bad_weeks={new_bad_weeks}",
                        evidence=f"realized_profit_usd={loop_realized_profit}, beats_previous_week={loop_beats}",
                    ),
                )

            if allocation:
                allocation_decisions[loop] = {
                    "allocation": allocation,
                    "consecutive_bad_weeks": new_bad_weeks,
                }

            _append_jsonl_atomic(
                score_history_path,
                {"ts": ts_now, "loop": loop, "realized_profit_usd": loop_realized_profit},
            )

    # (9): THE single loop-registry.json write for this pass -- INV-CEO-2.
    next_registry = allocator.build_next_registry(
        existing_registry, budget_snapshot_by_loop, rollback_restore, allocation_decisions
    )
    allocator.write_registry_atomic(registry_path, next_registry)

    # (10): miss-streak persistence -- the only place cooldown_weeks_remaining is written.
    cooldown_next = allocator.next_cooldown_weeks_remaining(
        cooldown_in, rollback_fired, rollback_cooldown_weeks=1
    )
    count_persisted = 0 if rollback_fired else count_new
    _write_json_atomic(
        miss_streak_path,
        {"consecutive_miss_count": count_persisted, "cooldown_weeks_remaining": cooldown_next},
    )

    # (11): verification row -- one canonical row per WEEKLY pass.
    row = allocator.build_verification_row(
        ts=ts_now,
        week_start=today,
        prev=prev_week_score,
        this=this_week_score,
        beats=beats,
        alloc_ref=f"loop-registry.json@{ts_now}",
        consecutive_miss_count=count_persisted,
        cooldown_weeks_remaining=cooldown_next,
        rollback_fired=rollback_fired,
        rolled_back_to_week=(today if rollback_fired else None),
    )
    _append_jsonl_atomic(verification_path, row)

    # (12): mail report -- best-effort, must never fail the pass.
    actions_summary = (
        ", ".join(f"{loop}:{d.get('allocation')}" for loop, d in allocation_decisions.items())
        or "none"
    )
    verification_summary = (
        f"beats_previous_week={str(beats).lower()}, rollback_fired={str(rollback_fired).lower()}"
    )
    report_args = allocator.build_ceo_report_args(this_week_score, actions_summary, verification_summary)
    evidence = allocator.build_evidence_pointer(
        verification_path if roster else None, today if roster else None
    )
    _send_mail_best_effort(
        report_script, "ceo", "weekly pass",
        report_args["result"], str(report_args["earned_usdc"]), evidence,
    )

    _write_json_atomic(weekly_meta_path, {"last_ceo_run_jst_date": today})
    print(
        json.dumps(
            {
                "pass": "weekly",
                "today": today,
                "company_score": this_week_score,
                "rollback_fired": rollback_fired,
                "allocation_decisions": allocation_decisions,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
