"""allocator.py — CEO WEEKLY pass pure functions (REQ-CEO-001/030..058/070..082). This module is the
single assembly point for `loop-registry.json` (INV-CEO-2, `build_next_registry`) and the single
shared currency-conversion path (INV-CEO-1, `realized_profit_usd`). It reuses existing purity-tested
modules rather than re-deriving their logic (`~/.claude/rules/building-effective-ai-agents.md`,
`車輪の再発明禁止`): `guardrails.py` for fleet-scaling gates (REQ-CEO-031), `cadence.py`/
`cadence-contracts.json` for roster/streak (read by the Tier2 caller, not this module).

B9 (道A, team-lead指示 2026-07-08): no `registry_updates` shared-mutable accumulator. Each WEEKLY-pass
step returns its own local, independent value (`budget_snapshot_by_loop` / `rollback_restore` /
`allocation_decisions`); `build_next_registry` is the only function that assembles them, once, into
the next `loop-registry.json`."""
from __future__ import annotations

import datetime
import json
import os
import sys

# REQ-CEO-031: reuse the existing loop-scale guardrail triad -- do not re-derive the 3-condition AND
# or re-hardcode any of its own internal threshold literals in this file.
_LOOP_SCALE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "loop-scale"
)
if _LOOP_SCALE_DIR not in sys.path:
    sys.path.insert(0, _LOOP_SCALE_DIR)
from guardrails import cooldown_ok, fleet_at_capacity, scale_eligible  # noqa: E402


# ============================================================================
# A. roster derivation (REQ-CEO-001, B1修正)
# ============================================================================
def derive_roster(cadence_contract: dict) -> list:
    """Dict-valued keys only (type filter, never a key-name exclusion list -- `_comment`'s value is
    a plain str and is excluded by `isinstance` before any `["kind"]` access happens, so a non-dict
    value can never raise). `founder-loop` (CEO's own body) is dropped."""
    loops = [k for k, v in cadence_contract.items() if isinstance(v, dict)]
    loops = [loop for loop in loops if loop != "founder-loop"]
    return loops


# ============================================================================
# B. currency conversion (REQ-CEO-002(c)/050, INV-CEO-1's single shared conversion path)
# ============================================================================
def _safe_float(value, default: float = 0.0) -> float:
    """Coerce a ledger/cost-event field to float; a wrong-type value (non-numeric string, nested
    dict/list, etc.) inside an otherwise-valid dict row returns `default` instead of raising (Phase 5
    hardening Finding F5 -- F1's real-ledger wiring made this a LIVE, externally-written-data
    corruption surface that a fabricated always-empty ledger path could never actually reach)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sum_earn_by_currency(rows: list) -> list:
    """Always exactly 2 entries (usd, jpy) -- never a fallback-field-winner branch. usd entry =
    sum(earn_usdc); jpy entry = sum(earn_jpy) + sum(commission_jpy). A row that parsed as valid JSON
    but is not itself a dict (e.g. a bare string/number/array/null line in a hand-edited or
    corrupted ledger) is skipped, not crashed on (Phase 5 hardening Finding F2 -- same defect class
    as the gig loop's GAP-1). A dict row whose numeric field holds a wrong-type value (e.g.
    `{"earn_usdc": "not-a-number"}`) contributes 0 for that field instead of crashing (Finding F5)."""
    dict_rows = [r for r in rows if isinstance(r, dict)]
    usd_total = sum(_safe_float(r.get("earn_usdc", 0) or 0) for r in dict_rows)
    jpy_total = sum(
        _safe_float(r.get("earn_jpy", 0) or 0) + _safe_float(r.get("commission_jpy", 0) or 0)
        for r in dict_rows
    )
    return [{"amount": usd_total, "currency": "usd"}, {"amount": jpy_total, "currency": "jpy"}]


def convert_to_usd(amount: float, currency: str, fx_config: dict) -> float:
    if currency == "usd":
        return float(amount)
    if currency == "jpy":
        return float(amount) / float(fx_config["jpy_usd_rate"])
    raise ValueError(f"unknown currency: {currency}")


def realized_profit_usd(entries: list, fx_config: dict) -> float:
    """THE single shared conversion path (INV-CEO-1) -- every `_usd`/`_usdc` parameter/field in this
    spec is required to be this function's return value, never a raw native-currency amount."""
    return float(sum(convert_to_usd(e["amount"], e["currency"], fx_config) for e in entries))


def company_score(per_loop_entries: dict, fx_config: dict) -> float:
    """Loop-cross-cutting sum of realized_profit_usd() -- the raw JPY figure never leaks unconverted
    into the total (each loop's entries are converted independently before summing)."""
    return float(sum(realized_profit_usd(entries, fx_config) for entries in per_loop_entries.values()))


# ============================================================================
# F. self-verification row (REQ-CEO-051, m5修正: canonical single row, 10 fields)
# ============================================================================
def build_verification_row(
    ts, week_start, prev, this, beats, alloc_ref,
    consecutive_miss_count, cooldown_weeks_remaining, rollback_fired, rolled_back_to_week,
) -> dict:
    return {
        "ts": ts,
        "week_start": week_start,
        "prev_week_company_score": prev,
        "this_week_company_score": this,
        "beats_previous_week": beats,
        "allocation_change_ref": alloc_ref,
        "consecutive_miss_count": consecutive_miss_count,
        "cooldown_weeks_remaining": cooldown_weeks_remaining,
        "rollback_fired": rollback_fired,
        "rolled_back_to_week": rolled_back_to_week,
    }


# ============================================================================
# D. double-down / scale-down gates (REQ-CEO-030/031/032)
# ============================================================================
def capital_increase_within_realized_profit(new_cap, old_cap, realized_profit_usd) -> bool:
    """REQ-CEO-030(b): a capital increase must stay within the loop's own realized profit range.
    Caller MUST pass this function's 3rd argument as the output of allocator.realized_profit_usd()
    (INV-CEO-1) -- the raw native-currency amount must never reach here directly (M4)."""
    increase = new_cap - old_cap
    if increase <= 0:
        return True
    return increase <= realized_profit_usd


def fleet_increase_allowed(
    streak, weekly_score, weekly_score_threshold, disk_free_gb,
    last_spawn_ts, now_ts, min_interval_days, current_count, max_count,
) -> bool:
    """REQ-CEO-031: fleet-size-increase gate = existing guardrails.py's 3-condition AND, called as-is
    (no re-derivation of scale_eligible/cooldown_ok/fleet_at_capacity's own thresholds)."""
    return (
        scale_eligible(streak, weekly_score, weekly_score_threshold, disk_free_gb)
        and cooldown_ok(last_spawn_ts, now_ts, min_interval_days)
        and not fleet_at_capacity(current_count, max_count)
    )


def should_scale_down(weekly_score, beats_previous_week, consecutive_bad_weeks) -> str:
    """REQ-CEO-032 (m3修正): 3-way branch. A "bad week" is score<=0 OR not beats_previous_week;
    `consecutive_bad_weeks>=2` escalates from a frequency cut to a full pause."""
    is_bad_week = (weekly_score <= 0) or (not beats_previous_week)
    if not is_bad_week:
        return "none"
    if consecutive_bad_weeks >= 2:
        return "pause"
    return "reduce_frequency"


def build_lesson_row(ts, loop, action, reason, evidence) -> dict:
    """REQ-CEO-034: {ts, loop, action, reason, evidence} for ceo-lessons.jsonl."""
    return {"ts": ts, "loop": loop, "action": action, "reason": reason, "evidence": evidence}


# ============================================================================
# E. loop-registry.json assembly (REQ-CEO-040..044, B7/B9修正: single assembly function)
# ============================================================================
def bootstrap_registry_if_missing(path: str) -> dict:
    """REQ-CEO-041: {} when the file doesn't exist yet (this feature may be its first writer,
    task #13's spawner hasn't run yet) -- never raises."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# REQ-CEO-042 (Phase 5 hardening Finding F4): shipped fail-CLOSED defaults for this feature's own
# 3 allocation fields, used by the caller (run_pass.py) whenever ceo-allocation-ranges.json does not
# exist on disk yet. Without this, validate_allocation_ranges()'s own documented "fields absent from
# ranges_cfg are not gated" contract makes a MISSING config file equivalent to no range gate at all
# (fail-open) -- directly contradicting behavioral-spec.md's own non-functional constraint that
# allocation anomalies must be fail-closed. A config file that DOES exist on disk still overrides
# these (operator intent wins); this constant only fills the gap when there is no file at all.
DEFAULT_ALLOCATION_RANGES = {
    "pass_frequency_multiplier": {"min": 0.1, "max": 10},
    "capital_cap_usd": {"min": 0, "max": 1_000_000},
    "fleet_size_target": {"min": 0, "max": 50},
}


def validate_allocation_ranges(allocation: dict, ranges_cfg: dict) -> bool:
    """REQ-CEO-042: reject (False) any field whose value is outside its configured [min, max]
    (inclusive). Fields absent from `ranges_cfg` are not gated."""
    for field_name, value in allocation.items():
        bounds = ranges_cfg.get(field_name)
        if not bounds:
            continue
        lo = bounds.get("min")
        hi = bounds.get("max")
        if lo is not None and value < lo:
            return False
        if hi is not None and value > hi:
            return False
    return True


def build_next_registry(
    existing_registry: dict,
    budget_snapshot_by_loop: dict,
    rollback_restore,
    allocation_decisions: dict,
) -> dict:
    """REQ-CEO-044 (B7新設, B9修正: 道A) -- THE sole assembly point for the next loop-registry.json,
    replacing the discarded `registry_updates` accumulator. Explicit per-field priority, computed
    once per loop, no shared mutable state between steps:
      - "budget": budget_snapshot_by_loop[loop] if present, else existing_registry[loop]["budget"].
      - "allocation": rollback_restore[loop]["allocation"] (if rollback_restore is not None and loop
        is in it) ELSE allocation_decisions[loop]["allocation"] (if present) ELSE
        existing_registry[loop]["allocation"].
      - "consecutive_bad_weeks": allocation_decisions[loop]["consecutive_bad_weeks"] if present, else
        existing_registry[loop]["consecutive_bad_weeks"].
    All other existing keys (e.g. "fleet", written by task #13's future spawner) are preserved
    unchanged. The loop set is the union of every dict's keys (this function has no separate roster
    argument -- callers pass a WEEKLY pass's own roster-scoped dicts, REQ-CEO-058 step 9)."""
    loops = set(existing_registry.keys()) | set(budget_snapshot_by_loop.keys()) | set(allocation_decisions.keys())
    if rollback_restore:
        loops |= set(rollback_restore.keys())

    next_registry = {}
    for loop in loops:
        existing_loop = existing_registry.get(loop, {})
        entry = dict(existing_loop)

        entry["budget"] = budget_snapshot_by_loop.get(loop, existing_loop.get("budget"))

        if rollback_restore and loop in rollback_restore:
            entry["allocation"] = rollback_restore[loop]["allocation"]
        else:
            entry["allocation"] = allocation_decisions.get(loop, {}).get(
                "allocation", existing_loop.get("allocation")
            )

        entry["consecutive_bad_weeks"] = allocation_decisions.get(loop, {}).get(
            "consecutive_bad_weeks", existing_loop.get("consecutive_bad_weeks")
        )

        next_registry[loop] = entry
    return next_registry


def write_registry_atomic(path: str, registry: dict) -> None:
    """REQ-CEO-044: tmp-write + os.replace, THE only I/O write of loop-registry.json per WEEKLY pass."""
    tmp = f"{path}.tmp.{os.getpid()}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ============================================================================
# F. cooldown / rollback state machine (REQ-CEO-052..058, B3/B4/B5/B6/B9修正)
# ============================================================================
def should_snapshot(consecutive_miss_count, cooldown_weeks_remaining) -> bool:
    """REQ-CEO-052 (B3/m4修正): only a fully healthy state (no miss streak, no cooldown) snapshots
    ceo-rollback.json -- freezes it during any in-progress streak or cooldown."""
    return consecutive_miss_count == 0 and cooldown_weeks_remaining == 0


def restore_from_rollback(rollback_snapshot: dict) -> dict:
    """REQ-CEO-053 (B7/B9修正): identity passthrough of the {loop: {"allocation": ...}} snapshot --
    no merge with anything else happens here (build_next_registry does the merge, once, at step 9)."""
    return rollback_snapshot


def should_rollback(consecutive_miss_count, cooldown_weeks_remaining, threshold: int = 2) -> bool:
    """REQ-CEO-053 (B3修正): fires only at/above `threshold` consecutive misses AND only when not
    already inside a cooldown window (cooldown blocks re-firing even at threshold)."""
    return consecutive_miss_count >= threshold and cooldown_weeks_remaining == 0


def update_miss_count(prev_count, beats_this_week: bool, cooldown_weeks_remaining) -> int:
    """REQ-CEO-054 (B5修正): during cooldown, returns `prev_count` UNCHANGED (never forced to 0).
    Outside cooldown: a beat resets to 0, a miss increments."""
    if cooldown_weeks_remaining > 0:
        return prev_count
    if beats_this_week:
        return 0
    return prev_count + 1


def next_cooldown_weeks_remaining(
    cooldown_weeks_remaining_in, rollback_fired_this_pass: bool, rollback_cooldown_weeks: int = 1
) -> int:
    """REQ-CEO-058 (B4新設): THE only function that decides the next-pass value of
    cooldown_weeks_remaining -- fixes the B4 set-then-decrement same-pass race. A rollback firing
    this pass ARMS the cooldown to `rollback_cooldown_weeks` (not decremented within the same pass
    that set it); otherwise an already-active cooldown decrements by 1; otherwise stays at 0."""
    if rollback_fired_this_pass:
        return rollback_cooldown_weeks
    if cooldown_weeks_remaining_in > 0:
        return max(0, cooldown_weeks_remaining_in - 1)
    return 0


# ============================================================================
# F. weekly-due gate (REQ-CEO-071, m1修正: no private-symbol cross-module import)
# ============================================================================
def _monday_of(date_str: str) -> datetime.date:
    d = datetime.date.fromisoformat(date_str)
    return d - datetime.timedelta(days=d.weekday())


def is_ceo_weekly_due(last_ceo_run_jst_date, today_jst_date) -> bool:
    """Monday-origin week gate: due when today's JST week (Mon start) is strictly later than the
    last WEEKLY pass's week, or when there has never been a run (`last_ceo_run_jst_date is None`)."""
    if last_ceo_run_jst_date is None:
        return True
    return _monday_of(today_jst_date) > _monday_of(last_ceo_run_jst_date)


# ============================================================================
# F. escalation schema gate (REQ-CEO-060/061: presence only, never content judgment)
# ============================================================================
def validate_escalation_schema(record: dict) -> bool:
    """REQ-CEO-060/061: gate is presence-only (non-empty `justification`) -- content is never judged
    by this deterministic gate, that judgment belongs to the agent (REQ-CEO-061)."""
    return bool(record.get("justification"))


# ============================================================================
# F. reporting args (REQ-CEO-080/081, report-args.mjs::founderReportArgs pattern)
# ============================================================================
def build_ceo_report_args(company_score, actions_summary, verification_summary) -> dict:
    """REQ-CEO-080: earned_usdc mirrors company_score directly (no independent recompute --
    company_score is already the INV-CEO-1-converted USD total)."""
    summary = f"actions: {actions_summary}; verification: {verification_summary}"
    if company_score > 0:
        return {"result": "success", "earned_usdc": company_score, "summary": summary}
    return {"result": "queue-empty", "earned_usdc": 0, "summary": summary}


def build_evidence_pointer(verification_jsonl_path, week_start) -> str:
    """REQ-CEO-081: a concrete pointer string when the roster is non-empty (satisfies loop-report.sh's
    `lr_valid_evidence` gate, which rejects a bare "none"); the exact `"none: ..."` sentence when the
    roster is empty (also accepted by that gate, per its `"none: <理由>"` allowance)."""
    if not verification_jsonl_path or not week_start:
        return "none: no loops registered yet"
    return f"{verification_jsonl_path}@{week_start}"
