#!/usr/bin/env python3
"""decision_loop.py — ONE scheduled cycle of the polymarket-trade loop, with reasoned decision
logging + Telegram reporting. THIS IS THE CORE DELIVERABLE of the 2026-07-25 decision-loop task.

Does NOT redesign or reimplement any strategy. It orchestrates the EXISTING, unchanged edge
logic (bundle_arb.py, market_maker.py, pinnacle_edge.py/pinnacle_observe.py, pick.py,
place_order.py) exactly as run.sh already chains them, and adds three things run.sh's trace
jsonl did not have:
  1. ONE unified per-cycle decision record (state/pm-decisions.jsonl, append-only) that captures,
     for every strategy, what was examined, the computed edge (incl. Pinnacle fair value vs
     Polymarket price), the position size it would take, and the decision WITH its reason —
     including every no_trade, with the specific reason, even when nothing happened at all.
  2. A Telegram push of that same record every cycle (reuses skills/_shared/send-telegram.sh —
     no new transport).
  3. A daily-loss circuit breaker check (daily_loss_guard.py) run BEFORE anything else, on top
     of the existing lifetime-cumulative guard (check_cumulative_halt) and the existing KILL file.

DRY BY DEFAULT (money-safety, HARD per the 2026-07-25 task brief): this script always exports
PM_DRY_RUN=1 to every child strategy process unless the CALLER's own environment already set
PM_DRY_RUN=0 AND PM_LIVE_CONFIRM=I_UNDERSTAND_THE_RISK is also set — a deliberate double-opt-in
so a stray `export PM_DRY_RUN=0` elsewhere on the box can never silently flip this loop live.
This script never sets either of those itself. Going live is a human decision (see SKILL.md's
"legal pivot" note on Polymarket-from-Japan) executed by hand, not by this file.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SKILL_DIR)  # so `import pinnacle_edge` / `import pinnacle_observe` resolve
STATE_DIR = os.path.join(SKILL_DIR, "..", "state")
DECISIONS_PATH = os.path.join(STATE_DIR, "pm-decisions.jsonl")
KILL_FILE = os.environ.get(
    "PM_KILL_SWITCH", os.path.expanduser("~/.local/state/life-manager/polymarket/KILL")
)
TELEGRAM_SCRIPT = os.path.join(SKILL_DIR, "..", "..", "_shared", "send-telegram.sh")
LEDGER_PATH = os.path.expanduser("~/anicca/skills/earn/state/earn-ledger.jsonl")

AGENT_HOME = os.environ.get(
    "PM_TRADE_AGENT_HOME", os.path.expanduser("~/.anicca-founder/agents/polymarket-agent")
)
VENV_PY = os.path.join(AGENT_HOME, ".venv", "bin", "python")

import daily_loss_guard  # noqa: E402  (same-dir import, needs SKILL_DIR on sys.path first)
from state_paths import external_state_path  # noqa: E402


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _live_confirmed() -> bool:
    """Double-opt-in gate. Both must be set by something OTHER than this script for a live
    strategy invocation to happen. Absence of either -> dry, no matter what."""
    return (
        os.environ.get("PM_DRY_RUN") == "0"
        and os.environ.get("PM_LIVE_CONFIRM") == "I_UNDERSTAND_THE_RISK"
    )


def child_env() -> dict:
    env = dict(os.environ)
    env["PM_TRADE_AGENT_HOME"] = AGENT_HOME
    if not _live_confirmed():
        env["PM_DRY_RUN"] = "1"
    # same BRAIN ENV run.sh exports for pick.py's consensus analyzer
    env.setdefault("OPENAI_BASE_URL", "http://127.0.0.1:8402/v1")
    env.setdefault("OPENAI_API_KEY", "x402-local")
    env.setdefault("BLOCKRUN_API_URL", "http://127.0.0.1:8402/v1")
    env.setdefault("BLOCKRUN_BASE_URL", "http://127.0.0.1:8402/v1")
    return env


def run_py(script_name: str, timeout: int, extra_env: dict | None = None) -> dict:
    """Run one strategy script under the agent venv, exactly like run.sh does. Never raises —
    a timeout/crash is data (an error record), not a fatal loop failure."""
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    path = os.path.join(SKILL_DIR, script_name)
    env = child_env()
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(
            [py, path], cwd=SKILL_DIR, env=env, capture_output=True, text=True, timeout=timeout
        )
        return {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr[-4000:]}
    except subprocess.TimeoutExpired as e:
        return {
            "exit": None,
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": f"TIMEOUT after {timeout}s",
        }
    except Exception as e:
        return {"exit": None, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------------------------
# deterministic bookkeeping: extract the ALREADY-PRINTED decision line, never invent one
# ---------------------------------------------------------------------------------------------
NO_TRADE_MARKERS = (
    "no risk-free bundle arb",
    "HOLD:",
    "no profitable maker-bundle market found",
    "naked leg handled this pass",
)
WOULD_TRADE_MARKERS = (
    "ARB FOUND:",
    "[DRY] would create_market_order",
    "[DRY] would post_order",
)
NAKED_WARNING_MARKERS = ("[DRY] would NAKED-FIX",)


def classify(stdout: str, exit_code: int | None) -> dict:
    lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
    if exit_code not in (0, None) or exit_code is None:
        pass  # fall through — still try to classify partial output before calling it an error
    naked_warns = [ln.strip() for ln in lines if any(m in ln for m in NAKED_WARNING_MARKERS)]
    would_trade = [ln.strip() for ln in lines if any(m in ln for m in WOULD_TRADE_MARKERS)]
    no_trade = [ln.strip() for ln in lines if any(m in ln for m in NO_TRADE_MARKERS)]
    scanned_match = re.search(r"scanned (\d+) markets", stdout or "")
    scanned = int(scanned_match.group(1)) if scanned_match else None
    if naked_warns:
        decision, reason = "no_trade", "REAL naked leg detected, dry mode did not flatten it: " + " | ".join(naked_warns)
    elif would_trade:
        decision, reason = "would_trade", " | ".join(would_trade)
    elif no_trade:
        decision, reason = "no_trade", " | ".join(no_trade)
    elif exit_code == 0:
        decision, reason = "no_trade", "no decision line matched (exit 0, see raw_tail)"
    else:
        decision, reason = "error", f"exit={exit_code}"
    return {
        "decision": decision,
        "reason": reason,
        "scanned_markets": scanned,
        "naked_leg_warning": bool(naked_warns),
        "raw_tail": lines[-10:],
    }


def run_pinnacle_observation() -> dict:
    """Pure observation — pinnacle_observe.py NEVER places an order (see its own docstring).
    Called in-process (stdlib only, no SDK deps) rather than via subprocess, and its own
    append_observation() is reused so state/pinnacle-observations.jsonl keeps accumulating
    exactly as SKILL.md documents; we additionally fold the SAME dict into our unified record."""
    try:
        import pinnacle_observe as po
    except Exception as e:
        return {"error": f"import failed: {e}", "funnel": {}, "edges": []}
    api_key = po.resolve_odds_api_key()
    if not api_key:
        return {"error": "ODDS_API_KEY not configured", "funnel": {}, "edges": []}
    obs = po.observe(api_key)
    po.append_observation(obs)
    return obs


def run_pick_and_maybe_order() -> dict:
    pick_res = run_py("pick.py", timeout=300)
    stdout = (pick_res["stdout"] or "").strip()
    try:
        pick = json.loads(stdout) if stdout else {"action": "WAIT", "reason": "empty-stdout"}
        if not isinstance(pick, dict):
            raise ValueError("not a dict")
    except Exception:
        pick = {"action": "WAIT", "reason": f"unparseable-pick-output:{stdout[-200:]!r}"}

    record = {"pick": pick, "place_order": None}
    if pick.get("action") == "WAIT":
        record["decision"] = "no_trade"
        record["reason"] = pick.get("reason", "unspecified")
        return record

    order_env = {
        "TOKEN_ID": str(pick.get("token_id", "")),
        "SIDE": str(pick.get("side", "BUY")),
        "AMOUNT": str(pick.get("amount", "")),
    }
    order_res = run_py("place_order.py", timeout=120, extra_env=order_env)
    order_stdout = (order_res["stdout"] or "").strip()
    try:
        order = json.loads(order_stdout) if order_stdout else {"ok": False, "error": "empty-stdout"}
    except Exception:
        order = {"ok": False, "error": f"unparseable:{order_stdout[-200:]!r}"}
    record["place_order"] = order
    record["decision"] = "would_trade" if order.get("dry_run") else ("trade" if order.get("ok") else "error")
    record["reason"] = (
        f"consensus={pick.get('consensus')} edge={pick.get('edge')} confidence={pick.get('confidence')} "
        f"market={pick.get('market')!r} amount=${pick.get('amount')}"
    )
    return record


def run_cycle() -> dict:
    ts = now_iso()
    cycle: dict = {"ts": ts, "dry_run": not _live_confirmed()}

    # money-safety guard #1: the SAME kill-switch every other entrypoint in this skill checks
    kill_file = external_state_path(KILL_FILE, Path(__file__).resolve().parents[3], "PM_KILL_SWITCH")
    if os.path.exists(kill_file):
        try:
            kill_reason = open(kill_file).read().strip() or "(empty KILL file)"
        except Exception:
            kill_reason = "(unreadable KILL file)"
        cycle.update({
            "action": "skip", "reason": f"kill-switch active: {kill_reason}",
            "strategies_run": [],
        })
        return cycle

    # money-safety guard #2 (NEW, 2026-07-25): daily-scoped circuit breaker, on top of the
    # existing lifetime-cumulative one (redeem.py/merge.py's check_cumulative_halt).
    dlg = daily_loss_guard.run_check(ledger_path=LEDGER_PATH, trip_kill_switch=True)
    cycle["daily_loss_guard"] = dlg
    if dlg["halted"]:
        cycle.update({"action": "skip", "reason": dlg["reason"], "strategies_run": []})
        return cycle

    strategies_run = []

    pinnacle = run_pinnacle_observation()
    strategies_run.append("pinnacle_observe")
    cycle["pinnacle"] = pinnacle

    arb_res = run_py("bundle_arb.py", timeout=200)
    strategies_run.append("bundle_arb")
    cycle["bundle_arb"] = {**classify(arb_res["stdout"], arb_res["exit"]), "exit": arb_res["exit"],
                            "stderr_tail": (arb_res["stderr"] or "")[-500:]}

    mm_res = run_py("market_maker.py", timeout=200)
    strategies_run.append("market_maker")
    cycle["market_maker"] = {**classify(mm_res["stdout"], mm_res["exit"]), "exit": mm_res["exit"],
                              "stderr_tail": (mm_res["stderr"] or "")[-500:]}

    pick_record = run_pick_and_maybe_order()
    strategies_run.append("pick")
    cycle["pick"] = pick_record

    cycle["strategies_run"] = strategies_run

    # top-level rollup: did ANY strategy this cycle decide it would trade?
    would_trade_any = (
        cycle["bundle_arb"]["decision"] == "would_trade"
        or cycle["market_maker"]["decision"] == "would_trade"
        or pick_record.get("decision") in ("would_trade", "trade")
    )
    cycle["action"] = "would_trade" if would_trade_any else "no_trade"

    # running P&L context (read-only, same ledger the daily guard reads)
    ledger_rows = daily_loss_guard.read_ledger_rows(LEDGER_PATH)
    pm_rows = [r for r in ledger_rows if str(r.get("source", "")).startswith("polymarket-")]
    cycle["pnl"] = {
        "cumulative_realized_usdc": round(sum(float(r.get("net_usdc", 0) or 0) for r in pm_rows), 6),
        "today_realized_usdc": dlg["today_net_usd"],
        "rows_counted": len(pm_rows),
    }
    return cycle


def append_decision(record: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(DECISIONS_PATH, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[decision_loop] WARNING: failed to append decision record: {e}", file=sys.stderr)


def format_telegram(record: dict) -> str:
    ts = record.get("ts", "?")
    mode = "DRY" if record.get("dry_run", True) else "LIVE"
    lines = [f"pm-trade [{mode}] {ts}"]

    if record.get("action") == "skip":
        lines.append(f"SKIPPED — {record.get('reason')}")
        return "\n".join(lines)

    pin = record.get("pinnacle", {})
    if pin.get("error"):
        lines.append(f"Pinnacle: error — {pin['error']}")
    else:
        funnel = pin.get("funnel", {})
        edges = pin.get("edges", [])
        lines.append(
            f"Pinnacle scan: {funnel.get('pinnacle_events', 0)} events, "
            f"{funnel.get('comparable', 0)} comparable, {len(edges)} edge(s) found"
        )
        for e in edges[:3]:
            lines.append(
                f"  {e.get('event')}: PM ${e.get('pm_price')} vs Pinnacle-fair {e.get('pinnacle_fair')} "
                f"-> edge {e.get('edge')} ({e.get('buy_outcome')})"
            )

    arb = record.get("bundle_arb", {})
    lines.append(f"bundle_arb: {arb.get('decision')} — {arb.get('reason')}")

    mm = record.get("market_maker", {})
    lines.append(f"market_maker: {mm.get('decision')} — {mm.get('reason')}")
    if mm.get("naked_leg_warning"):
        lines.append("  WARNING: a real naked (unhedged) leg was detected and NOT flattened (dry mode).")

    pick = record.get("pick", {})
    lines.append(f"pick(directional): {pick.get('decision')} — {pick.get('reason')}")

    pnl = record.get("pnl", {})
    lines.append(
        f"P&L: today ${pnl.get('today_realized_usdc', 0):+.2f}  "
        f"cumulative ${pnl.get('cumulative_realized_usdc', 0):+.2f}  "
        f"({pnl.get('rows_counted', 0)} realized rows)"
    )
    return "\n".join(lines)


def send_telegram(text: str) -> dict:
    if not os.path.exists(TELEGRAM_SCRIPT):
        return {"sent": False, "error": "send-telegram.sh not found"}
    try:
        p = subprocess.run(["bash", TELEGRAM_SCRIPT, text], capture_output=True, text=True, timeout=15)
        out = (p.stdout or "").strip()
        sent = out.startswith("TELEGRAM_SENT=true")
        msgid = None
        m = re.search(r"MSGID=(\d+)", out)
        if m:
            msgid = int(m.group(1))
        return {"sent": sent, "raw": out, "message_id": msgid, "exit": p.returncode}
    except Exception as e:
        return {"sent": False, "error": str(e)}


def main() -> int:
    record = run_cycle()
    telegram_text = format_telegram(record)
    tg_result = send_telegram(telegram_text)
    record["telegram"] = tg_result
    append_decision(record)

    print(telegram_text)
    print(f"[decision_loop] telegram: {tg_result}")
    print(f"[decision_loop] appended to {DECISIONS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
