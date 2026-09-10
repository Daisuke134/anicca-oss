#!/usr/bin/env python3
"""cadence-evidence.py — REQ-LV-104: gathers REAL evidence (ledger reads, file mtimes) for each of
the 7 Cadence Contract loops (clip/affiliate/video/gig/bounty/pm-earner/founder-loop) and calls the
pure `cadence.py` dispatcher on it. This is the IMPURE side of the purity boundary — cadence.py
itself never touches a file; this module is the only thing that does, and its job is narrowly
"read a real timestamp/value, hand it to the pure function," never "decide what health means."

CLI: `python3 cadence-evidence.py status <loop-name>` -> one JSON line:
  {"loop": "clip", "met": true, "streak": 3, "scorecard": "✅posted-today (streak=3)"}
Missing/not-yet-populated source files are NOT an error — they simply produce empty evidence, which
cadence_met() honestly evaluates as unmet (never fabricated, matches this codebase's fail-closed
convention across record_earn.py / loop-report.sh / record-earn.mjs).
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import zoneinfo
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cadence import cadence_met, streak  # noqa: E402

JST = zoneinfo.ZoneInfo("Asia/Tokyo")
REPO_ROOT = Path(os.environ.get("LIFE_MANAGER_REPO", Path(__file__).resolve().parents[2]))
CONTRACTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cadence-contracts.json")
STREAK_WINDOW_DAYS = 14  # how far back streak() looks; matches this codebase's other rolling-window conventions


def _load_contracts():
    with open(CONTRACTS_PATH) as f:
        d = json.load(f)
    d.pop("_comment", None)
    return d


def _epoch_to_jst_date(ts_seconds: float) -> str:
    return datetime.datetime.fromtimestamp(ts_seconds, tz=JST).date().isoformat()


def _gig_ts_to_jst_date(ts):
    """REQ-GFV-021 (PROP-029) — pure, gig-specific timestamp parser: converts a
    ~/gig/applied.jsonl / ~/gig/listings.jsonl row's `ts` field to a JST calendar-date string,
    tolerating a numeric UNIX epoch (int/float, same convention as _epoch_to_jst_date above), a
    digit-only STRING epoch (e.g. bash's `"ts":"$(date +%s)"` — a quoted number is still a number,
    just written by a shell that JSON-quotes everything; confirmed live 2026-07-11 when a nurture-
    step reply landed as `"ts": "1783772558"` and was silently invisible to the cadence check until
    this branch was added), AND an ISO-8601 string (with a `Z` suffix or an explicit +HH:MM/-HH:MM
    offset). Genuine parsing of a fixed, machine-generated timestamp format — not judgment. Never
    crashes: null/missing/unparseable input returns None (skipped, same tolerant-parse convention as
    every other ledger reader in this codebase). Does NOT modify the EXISTING _epoch_to_jst_date
    (clip/affiliate/video, numeric-epoch-only) — this is a new, separate function scoped to gig's
    evidence-gathering only.
    """
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        try:
            return _epoch_to_jst_date(ts)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(ts, str) and ts.strip():
        s = ts.strip()
        if s.isdigit():
            try:
                return _epoch_to_jst_date(int(s))
            except (OSError, OverflowError, ValueError):
                return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(JST).date().isoformat()
    return None


def _mtime_epoch(path: str):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _read_jsonl_rows(path: str):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    return rows


def _event_dates_from_ts_rows(rows, ts_field="ts"):
    dates = set()
    for r in rows:
        ts = r.get(ts_field)
        if isinstance(ts, (int, float)):
            dates.add(_epoch_to_jst_date(ts))
    return dates


# ---------------------------------------------------------------------------
# Per-loop evidence sources (the only place real paths are known)
# ---------------------------------------------------------------------------

def _clip_ledger_path():
    return os.environ.get("EARN_LEDGER") or os.path.expanduser("~/.local/state/life-manager/state/clip-earn-ledger.jsonl")


def _video_metrics_path():
    override = os.environ.get("EARN_VIDEO_METRICS_PATH")
    if override:
        return override
    handle = os.environ.get("EARN_VIDEO_HANDLE", "money_blueprintdaily")
    return os.path.expanduser(f"~/.cloak/earn-video-metrics-{handle}.jsonl")


def _affiliate_metrics_path():
    return os.environ.get("AFFILIATE_METRICS_PATH") or os.path.expanduser("~/.cloak/affiliate-metrics.jsonl")


def _gig_funnel_path():
    # NOTE (REQ-GFV-023, iteration-2 rewrite): no longer used by the `gig` cadence-evidence path
    # below (moved to _gig_activity_event_dates / applied.jsonl+listings.jsonl instead) — kept in
    # place, unused by cadence, since gig-funnel.jsonl remains a valid file consumed elsewhere (the
    # WEEKLY EDD evaluator, an entirely separate concern from the DAILY cadence bar this fixes).
    return os.environ.get("GIG_FUNNEL_PATH") or os.path.expanduser("~/gig/gig-funnel.jsonl")


def _gig_applied_path():
    return os.environ.get("GIG_APPLIED_PATH") or os.path.expanduser("~/gig/applied.jsonl")


def _gig_listings_path():
    return os.environ.get("GIG_LISTINGS_PATH") or os.path.expanduser("~/gig/listings.jsonl")


# Local literal copy of funnel.py's won/paid status vocabulary (REQ-GFV-022) — copied, not
# cross-repo-imported, since funnel.py lives in __REPO_ROOT__/ while this module lives in
# __REPO_ROOT__/; documented as intentionally mirroring funnel.py's vocabulary so the two stay in
# lockstep if either changes.
_GIG_WON_STATUSES = {"受注", "成約"}
_GIG_PAID_STATUSES = {"検収完了", "支払"}
_GIG_REAL_ACTION_STATUSES = {"applied", "replied"} | _GIG_WON_STATUSES | _GIG_PAID_STATUSES


def _gig_activity_event_dates(applied_rows, listings_rows):
    """REQ-GFV-022 (PROP-030, PROP-036) — pure: given already-read applied_rows/listings_rows (the
    file reads are the effectful shell, see _gig_applied_path/_gig_listings_path below), returns the
    union of (a) JST dates (via _gig_ts_to_jst_date) of every applied_rows entry whose `status` is
    EXACTLY a member of _GIG_REAL_ACTION_STATUSES (an EXACT-MATCH filter — deliberately not a
    prefix/substring match, since a real "applied_0" status string — a zero-viable-candidates scan
    summary, NOT a real application — would otherwise be wrongly counted), and (b) the JST date of
    each listings_rows entry's FIRST-EVER appearance for its `listing_id` (a later price-update/
    delist row for an already-seen listing_id does not contribute an additional date). Never
    crashes: rows with unparseable/missing ts, or missing listing_id, are skipped."""
    dates = set()
    for row in applied_rows or []:
        if not isinstance(row, dict):  # GAP-1: a bare non-dict JSON line (agent-written, append-only ledger) must be skipped, not crash
            continue
        if row.get("status") not in _GIG_REAL_ACTION_STATUSES:
            continue
        d = _gig_ts_to_jst_date(row.get("ts"))
        if d:
            dates.add(d)

    first_seen_by_listing_id = {}
    for row in listings_rows or []:
        if not isinstance(row, dict):  # GAP-1: skip non-dict listings rows defensively
            continue
        listing_id = row.get("listing_id")
        if listing_id is None:
            continue
        d = _gig_ts_to_jst_date(row.get("ts"))
        if d and listing_id not in first_seen_by_listing_id:
            first_seen_by_listing_id[listing_id] = d
    dates.update(first_seen_by_listing_id.values())

    return dates


def _gig_row_exists_event_dates():
    applied_rows = _read_jsonl_rows(_gig_applied_path())
    listings_rows = _read_jsonl_rows(_gig_listings_path())
    return _gig_activity_event_dates(applied_rows, listings_rows)


# Test-only override seams (mirrors this codebase's own EARN_LEDGER/FOUNDER_DIR/FOUNDER_TEST
# convention, e.g. record_earn.py/founder-loop.sh) so a Tier2 test can exercise this module's REAL
# evidence-gathering logic against real temp fixture files instead of production paths — never
# reaches for a mock of cadence_met/streak themselves (those stay pure and untouched).
LEDGER_PATH_FOR_LOOP = {
    "clip": _clip_ledger_path,
    "affiliate": _affiliate_metrics_path,
    "video": _video_metrics_path,
}


def _row_exists_event_dates(loop):
    path = LEDGER_PATH_FOR_LOOP[loop]()
    rows = _read_jsonl_rows(path)
    dates = _event_dates_from_ts_rows(rows)
    if dates:
        return dates
    # FALLBACK (clip's existing ledger predates this feature and carries no per-row `ts` field —
    # see self_heal.py's ledger writer, confirmed 2026-07-08). The new metrics/funnel files this
    # feature introduces for affiliate/video/gig (REQ-LV-013/015) DO carry `ts`; this branch only
    # ever fires for a ledger with real rows but none of them timestamped, so the file's own mtime
    # is the most honest single-day signal available — never fabricates a date beyond "this file
    # was genuinely written on this JST calendar day."
    if rows:
        m = _mtime_epoch(path)
        if m is not None:
            return {_epoch_to_jst_date(m)}
    return dates


def _video_state_path():
    override = os.environ.get("EARN_VIDEO_STATE_PATH")
    if override:
        return override
    handle = os.environ.get("EARN_VIDEO_HANDLE", "money_blueprintdaily")
    return os.path.expanduser(f"~/.cloak/earn-video-{handle}.json")


def _video_audit_path():
    override = os.environ.get("EARN_VIDEO_AUDIT_PATH")
    if override:
        return override
    handle = os.environ.get("EARN_VIDEO_HANDLE", "money_blueprintdaily")
    return os.path.expanduser(f"~/.cloak/earn-video-audit-{handle}.jsonl")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# ★★★ GUARDED EXIT-CRITERIA INVARIANT (self-heal.md root cause #1, fixed 2026-07-11) ★★★
# commit ab4a39a4 (a self-fix pass) originally made _video_warmup_attempt_event_dates() count ANY
# S1_warmup audit line — including "INCOMPLETE...day NOT advanced" — as the day's contracted work
# done. That is Goodhart's law in code: it stopped the daily false-misfire alert by lowering the
# bar the alert measures against, not by fixing why warmup wasn't progressing. Real production data
# proved it: state.warmup_day sat at 4 for 4 straight days (07-08..07-11) while this function still
# reported ✅posted-today (streak=2) every one of those days — a genuinely stalled loop read as
# healthy. See docs/superpowers/evidence/self-heal.md for the full incident.
#
# THE INVARIANT THIS FILE MUST NEVER VIOLATE AGAIN: a day where the warmup did NOT actually advance
# (state.warmup_day did not increase) must NEVER be counted as cadence-met, no matter how the browser
# session's outcome is worded. "The loop tried" is not "the loop's contracted work happened" — only
# _video_warmup_advanced_event_dates() (exit-criteria: real day-advance) may feed cadence's
# event_dates. Any change that widens this back toward "any attempt counts" is a bar-lowering change
# and requires fresh-context (Opus) adversary review before merge — self-fix must NOT loosen this
# unilaterally. test_cadence_evidence.py's "day NOT advanced -> met=false" fixture locks this in;
# do not edit that assertion without the same review.
def _video_warmup_attempted_event_dates():
    """Diagnostic-only: every JST date carrying ANY real 'S1_warmup' audit line, regardless of
    outcome (success, incomplete, deferred-browser, ban-backoff). Proves the loop drove a real
    browser session that day. NEVER used to satisfy cadence on its own — see the GUARDED INVARIANT
    above. Exposed in gather_evidence()'s evidence dict purely so a human/adversary reading
    `cadence-evidence.py status video` output can see "attempted but did not advance" instead of the
    old invisible-diagnosis failure mode (self-heal.md root cause #2)."""
    dates = set()
    for row in _read_jsonl_rows(_video_audit_path()):
        if not isinstance(row, dict):
            continue
        if row.get("transition") != "S1_warmup":
            continue
        d = row.get("date")
        if isinstance(d, str) and d:
            dates.add(d)
    return dates


def _video_warmup_advanced_event_dates():
    """Exit-criteria check (self-heal.md fix #1): a JST date counts toward cadence ONLY if that
    day's audit ledger shows warmup_day actually advanced — never merely "a browser session ran".
    run.sh (the ONLY writer of this ledger) emits exactly one literal template on the success path,
    `DID="warmup day→$(wday) (real reels watched=$WATCHED)"` (skills/earn/video/run.sh) — every other
    outcome (INCOMPLETE/deferred/STOPPED) explicitly says the day was NOT advanced. Matching the
    literal 'day→' arrow this one template writes is parsing a fixed machine format the same
    deterministic bash code always produces (never an LLM judgment call — the building-effective-
    agents rule's carve-out for parsing, not judgment), and is a positive allowlist match (fail-
    closed): an unrecognized future outcome string does NOT count as advanced by default, it must
    explicitly earn the 'day→' marker. Never fabricates a date beyond what the ledger recorded."""
    dates = set()
    for row in _read_jsonl_rows(_video_audit_path()):
        if not isinstance(row, dict):
            continue
        if row.get("transition") != "S1_warmup":
            continue
        did = row.get("did")
        if not isinstance(did, str) or "day→" not in did:
            continue
        d = row.get("date")
        if isinstance(d, str) and d:
            dates.add(d)
    return dates


def _video_row_exists_event_dates():
    """video's cadence source is normally the strict real-POST ledger (_row_exists_event_dates,
    REQ-LV-013) — correct once the account is actually posting. While state.status == 'warming'
    (still bootstrapping), a day where warmup GENUINELY ADVANCED
    (_video_warmup_advanced_event_dates — the exit-criteria fix, NOT mere attempt) satisfies the day
    instead; the moment warmup completes and status flips away from 'warming', this reverts to the
    strict post-based check unconditionally, so a stalled or lazy POST-posting account is still
    caught. See the GUARDED INVARIANT comment above _video_warmup_attempted_event_dates: this must
    stay 'advanced', never regress to 'attempted'."""
    post_dates = _row_exists_event_dates("video")
    state = _read_json(_video_state_path())
    if state.get("status") == "warming":
        return post_dates | _video_warmup_advanced_event_dates()
    return post_dates


def _bounty_funnel_path():
    return os.environ.get("BOUNTY_FUNNEL_PATH") or str(
        REPO_ROOT / "skills/human-funded/bounty/state/bounty-funnel.jsonl")


def _bounty_funnel_rows():
    return _read_jsonl_rows(_bounty_funnel_path())


def _bounty_today_and_previous_checked(today_jst_date):
    rows = _bounty_funnel_rows()
    by_date = {}
    for r in rows:
        ts = r.get("ts")
        checked = r.get("checked")
        if isinstance(ts, (int, float)) and isinstance(checked, (int, float)):
            d = _epoch_to_jst_date(ts)
            # keep the LATEST value seen for each JST day (pass-order in the file = chronological)
            by_date[d] = checked
    today_value = by_date.get(today_jst_date, 0)
    prior_dates = sorted(d for d in by_date if d < today_jst_date)
    previous_value = by_date[prior_dates[-1]] if prior_dates else 0
    return today_value, previous_value, by_date


def _founder_state_md_path():
    # BUG FIX (2026-07-11 self-fix, cadence audit false-daily-miss): founder-loop.sh (see
    # skills/self/founder-loop/founder-loop.sh's STATE_MD="$DIR/STATE.md") has ALWAYS written to
    # ~/.anicca-founder/STATE.md, never .../state/STATE.md — confirmed via `git log -p --follow`
    # back to that script's very first commit, and documented correctly in
    # skills/self/launchd/README.md. This function's default had the wrong path since ITS OWN
    # first commit too, so founder-loop's Cadence Contract's marker_jst_date was always None and
    # cadence_met() always returned False here — every 21:00 JST cadence-deadline-check.sh
    # escalated a false "NOT met" self-fix for founder-loop regardless of real earn activity.
    return os.environ.get("FOUNDER_STATE_MD_PATH") or os.path.expanduser(
        "~/.local/state/life-manager/founder-loop-cadence/STATE.md"
    )


def _founder_loop_marker_jst_date():
    m = _mtime_epoch(_founder_state_md_path())
    return _epoch_to_jst_date(m) if m is not None else None


def _pm_earner_log_path():
    return os.environ.get("PM_EARNER_LOG_PATH") or str(
        REPO_ROOT / "skills/earn/polymarket-trade/earner.log")


def _pm_earner_ledger_path():
    return os.environ.get("PM_EARNER_LEDGER_PATH") or str(
        REPO_ROOT / "skills/earn/state/earn-ledger.jsonl")


def _pm_earner_earner_log_epoch():
    return _mtime_epoch(_pm_earner_log_path())


def _pm_earner_redeem_event_dates():
    rows = _read_jsonl_rows(_pm_earner_ledger_path())
    redeem_rows = [r for r in rows if r.get("source") == "polymarket-redeem" or "redeem" in str(r.get("task", "")).lower()]
    return _event_dates_from_ts_rows(redeem_rows)


def gather_evidence(loop: str, today_jst_date: str, now_epoch_seconds: float) -> dict:
    if loop in ("clip", "affiliate"):
        return {"event_dates": sorted(_row_exists_event_dates(loop))}

    if loop == "video":
        # event_dates drives cadence_met (exit-criteria: advanced-only, see GUARDED INVARIANT
        # above _video_warmup_attempted_event_dates). attempted_dates/advanced_dates are NOT
        # consumed by cadence.py's pure row-exists dispatch — they ride along purely so a human or
        # adversary reading this JSON directly sees "attempted but did not advance" instead of the
        # old invisible-diagnosis failure mode (self-heal.md root cause #2).
        return {
            "event_dates": sorted(_video_row_exists_event_dates()),
            "warmup_attempted_dates": sorted(_video_warmup_attempted_event_dates()),
            "warmup_advanced_dates": sorted(_video_warmup_advanced_event_dates()),
        }

    if loop == "gig":
        return {"event_dates": sorted(_gig_row_exists_event_dates())}

    if loop == "bounty":
        today_value, previous_value, _ = _bounty_today_and_previous_checked(today_jst_date)
        return {"today_value": today_value, "previous_value": previous_value}

    if loop == "founder-loop":
        return {"marker_jst_date": _founder_loop_marker_jst_date()}

    if loop == "pm-earner":
        marker = _pm_earner_earner_log_epoch()
        return {
            "by_condition": {
                "hourly-pass": {
                    "marker_epoch_seconds": marker,
                    "now_epoch_seconds": now_epoch_seconds,
                },
                "daily-redeem": {"event_dates": sorted(_pm_earner_redeem_event_dates())},
            }
        }

    raise ValueError(f"no evidence source wired for loop: {loop!r}")


def evidence_by_date_for_streak(loop: str, today_jst_date: str, window_days: int = STREAK_WINDOW_DAYS) -> dict:
    """Build the evidence_by_date map streak() needs by re-gathering each day's own evidence. For
    row-exists loops this is cheap (same event_dates set, evaluated once per day in the window). For
    increment/pass-marker/compound this re-derives each day's evidence from the same underlying
    real sources — never fabricated, just re-sliced per day."""
    today = datetime.date.fromisoformat(today_jst_date)
    out = {}
    if loop in ("clip", "affiliate"):
        dates = _row_exists_event_dates(loop)
        for i in range(window_days):
            d = (today - datetime.timedelta(days=i)).isoformat()
            out[d] = {"event_dates": [d] if d in dates else []}
        return out

    if loop == "video":
        dates = _video_row_exists_event_dates()
        for i in range(window_days):
            d = (today - datetime.timedelta(days=i)).isoformat()
            out[d] = {"event_dates": [d] if d in dates else []}
        return out

    if loop == "gig":
        dates = _gig_row_exists_event_dates()
        for i in range(window_days):
            d = (today - datetime.timedelta(days=i)).isoformat()
            out[d] = {"event_dates": [d] if d in dates else []}
        return out

    if loop == "bounty":
        _, _, by_date = _bounty_today_and_previous_checked(today_jst_date)
        sorted_dates = sorted(by_date)
        for i in range(window_days):
            d = (today - datetime.timedelta(days=i)).isoformat()
            prior = [x for x in sorted_dates if x < d]
            out[d] = {
                "today_value": by_date.get(d, 0),
                "previous_value": by_date[prior[-1]] if prior else 0,
            }
        return out

    if loop == "founder-loop":
        marker = _founder_loop_marker_jst_date()
        for i in range(window_days):
            d = (today - datetime.timedelta(days=i)).isoformat()
            out[d] = {"marker_jst_date": marker}
        return out

    if loop == "pm-earner":
        marker = _pm_earner_earner_log_epoch()
        redeem_dates = _pm_earner_redeem_event_dates()
        now = datetime.datetime.now(tz=JST).timestamp()
        for i in range(window_days):
            d = (today - datetime.timedelta(days=i)).isoformat()
            out[d] = {
                "by_condition": {
                    "hourly-pass": {"marker_epoch_seconds": marker, "now_epoch_seconds": now},
                    "daily-redeem": {"event_dates": [d] if d in redeem_dates else []},
                }
            }
        return out

    raise ValueError(f"no evidence source wired for loop: {loop!r}")


def status_for_loop(loop: str) -> dict:
    contracts = _load_contracts()
    contract = contracts[loop]
    now_jst = datetime.datetime.now(tz=JST)
    today_jst_date = now_jst.date().isoformat()
    now_epoch = now_jst.timestamp()

    evidence = gather_evidence(loop, today_jst_date, now_epoch)
    met = cadence_met(today_jst_date, contract, evidence)
    evidence_by_date = evidence_by_date_for_streak(loop, today_jst_date)
    s = streak(evidence_by_date, today_jst_date, contract)

    scorecard = f"{'✅posted-today' if met else '❌missed'} (streak={s})"
    return {"loop": loop, "met": met, "streak": s, "scorecard": scorecard}


def _main():
    if len(sys.argv) < 3 or sys.argv[1] != "status":
        print("usage: cadence-evidence.py status <loop-name>", file=sys.stderr)
        sys.exit(2)
    loop = sys.argv[2]
    print(json.dumps(status_for_loop(loop), ensure_ascii=False))


if __name__ == "__main__":
    _main()
