#!/usr/bin/env python3
"""One finite Alpaca investment pass."""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from allocator import build_candidates, choose, gate as allocation_gate, order_for
from alpaca_cli import (CLI_OPERATIONS, SAFE_ERROR_CODES, find_order_by_client_id, observe,
                        read_allocator_snapshot, read_campaign_snapshot, submit_order)
from campaign import CANDIDATE_REF, SYMBOLS, exit_order, reconcile
from control import control_fence, read_control
from effect_store import (mark_started, reconcile_started, record_no_trade, seal,
                          unresolved_intent_count)
from reporter import deliver, deliver_control, deliver_failure
from position_manager import choose as choose_position, exit_order as live_exit_order
from review_status import read_receipt as read_application_status
from review_status import refresh as refresh_application_status
from risk_policy import evaluate_entry


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _retry_allowed(stage: str, effect_attempted: bool, attempt: int) -> bool:
    return stage != "telegram_deliver" and not effect_attempted and attempt < 2


def _terminal_effect(effect_attempted: bool) -> str:
    return "unknown" if effect_attempted else "none"


def _error_code(error: Exception) -> str:
    value = str(error)
    if value in SAFE_ERROR_CODES:
        return value
    for prefix in ("alpaca_cli_failed:", "alpaca_cli_timeout:"):
        if value.startswith(prefix) and value.removeprefix(prefix) in CLI_OPERATIONS:
            return value
    return type(error).__name__


def _deployment() -> str:
    value = os.environ.get("LIFE_MANAGER_INVESTMENT_DEPLOYMENT")
    if value not in {"local", "cloud"}:
        raise ValueError("investment_deployment_invalid")
    return value


def _mode() -> str:
    value = os.environ.get("LIFE_MANAGER_INVESTMENT_MODE")
    if value not in {"paper", "shadow", "live"}:
        raise ValueError("investment_mode_invalid")
    return value


def _mode_paths(mode: str) -> tuple[Path, Path]:
    if mode not in {"paper", "shadow", "live"}:
        raise ValueError("investment_mode_invalid")
    suffix = mode.upper()
    credentials = os.environ.get(f"ALPACA_INVESTMENT_{suffix}_CREDENTIALS_FILE")
    state = os.environ.get(f"ALPACA_INVESTMENT_{suffix}_STATE_DIR")
    if mode == "paper":
        credentials = credentials or os.environ.get("ANICCA_CREDENTIALS_FILE") \
            or "~/.local/share/anicca/credentials.json"
        state = state or os.environ.get("ALPACA_INVESTMENT_STATE_DIR") \
            or "~/.local/state/life-manager/alpaca-investment"
    elif not credentials or not state:
        raise ValueError("investment_mode_paths_missing")
    selected_state = Path(state).expanduser()
    selected_resolved = selected_state.resolve()
    paper_state = (os.environ.get("ALPACA_INVESTMENT_PAPER_STATE_DIR")
                   or os.environ.get("ALPACA_INVESTMENT_STATE_DIR")
                   or "~/.local/state/life-manager/alpaca-investment")
    for other, other_state in (
        ("paper", paper_state),
        ("shadow", os.environ.get("ALPACA_INVESTMENT_SHADOW_STATE_DIR")),
        ("live", os.environ.get("ALPACA_INVESTMENT_LIVE_STATE_DIR")),
    ):
        if other != mode and other_state and selected_resolved == Path(other_state).expanduser().resolve():
            raise ValueError("investment_mode_state_path_conflict")
    return Path(credentials).expanduser(), selected_state


def _review_status(state: Path, mode: str, deployment: str) -> dict:
    current = read_application_status(state)
    if mode != "paper" or deployment != "local" or not current:
        return current
    try:
        return refresh_application_status(state)
    except Exception:
        return current


def _nonpaper_campaign(observation: dict) -> dict:
    try:
        positions = observation["positions"]
        if not isinstance(positions, list):
            raise ValueError
        unrealized = sum((Decimal(str(row["unrealized_pl"])) for row in positions), Decimal("0"))
        if not unrealized.is_finite():
            raise ValueError
    except (KeyError, InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("investment_nonpaper_observation_invalid") from error
    return {"exit_status": "NOT_APPLICABLE", "paper": False,
            "positions": positions, "realized_pnl_usd": None,
            "unrealized_pnl_usd": str(unrealized)}


def _owned_live_position(state: Path, credentials_path: Path, cli_path: Path,
                         observation: dict) -> None:
    try:
        ownership = json.loads((state / "live-owned-position.json").read_text(encoding="utf-8"))
        qty = Decimal(str(observation["positions"][0]["qty"]))
    except (FileNotFoundError, json.JSONDecodeError, InvalidOperation, KeyError,
            IndexError, TypeError) as error:
        raise ValueError("live_position_not_owned") from error
    if ownership.get("status") != "open" or ownership.get("symbol") != "BTCUSD":
        raise ValueError("live_position_not_owned")
    broker = find_order_by_client_id(credentials_path=credentials_path, cli_path=cli_path,
                                     client_order_id=ownership.get("client_order_id", ""))
    try:
        filled = Decimal(str(broker["filled_qty"]))
    except (InvalidOperation, KeyError, TypeError) as error:
        raise ValueError("live_position_not_owned") from error
    if broker.get("status") != "filled" or qty <= 0 or qty > filled:
        raise ValueError("live_position_not_owned")


def main(*, attempt: int = 0, wake_id=None) -> int:
    wake_id = wake_id or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    mode = os.environ.get("LIFE_MANAGER_INVESTMENT_MODE")
    state = Path(os.environ.get("ALPACA_INVESTMENT_STATE_DIR",
                               "~/.local/state/life-manager/alpaca-investment")).expanduser()
    effect_attempted = False
    observation = None
    campaign = None
    stage = "start"
    try:
        mode = _mode()
        credentials_path, state = _mode_paths(mode)
        deployment = _deployment()
        cli_path = Path(os.environ.get("ALPACA_CLI", "~/.local/bin/alpaca")).expanduser()
        stage = "reconcile_started"
        reconciliation = reconcile_started(
            state / "receipts.jsonl",
            lambda client_order_id: find_order_by_client_id(
                credentials_path=credentials_path,
                cli_path=cli_path,
                client_order_id=client_order_id,
            ),
        )
        stage = "control_read"
        control = read_control(state / "control.json")
        if control["paused"] or control["killed"]:
            stage = "telegram_deliver"
            telegram = deliver_control(
                state, control=control, wake_id=wake_id, mode=mode)
            print(json.dumps({
                "effect": "none", "loop_id": "alpaca-investment", "mode": mode,
                "reconciliation": reconciliation,
                "status": "killed" if control["killed"] else "paused",
                "telegram_message_id": telegram["message_id"],
            }, separators=(",", ":")))
            return 0
        stage = "observe"
        observation = observe(
            credentials_path=credentials_path,
            cli_path=cli_path,
        )
        stage = "campaign_read"
        campaign = (reconcile(read_campaign_snapshot(
            credentials_path=credentials_path, cli_path=cli_path, symbols=SYMBOLS))
            if mode == "paper" else _nonpaper_campaign(observation))
        effect = "none"
        if campaign["exit_status"] == "EXIT_READY":
            exit_decision = {
                "candidate_ref": CANDIDATE_REF,
                "deployment": deployment,
                "mode": mode,
                "gate": "campaign_exit_ready",
                "paper": mode == "paper",
                "reason": "sealed_campaign_regular_session_positive_credit",
            }
            if mode != "paper":
                exit_decision.update({"approved": False, "gate": f"{mode}_read_only"})
                record_no_trade(state / "receipts.jsonl", exit_decision)
            else:
                exit_order_path = state / "campaign-exit-order.json"
                if exit_order_path.is_file():
                    stage = "campaign_exit_order_read"
                    order = json.loads(exit_order_path.read_text(encoding="utf-8"))
                else:
                    stage = "campaign_exit_order_build"
                    order = exit_order(campaign)
                    _atomic_json(exit_order_path, order)
                stage = "campaign_exit_submit"
                with control_fence(state) as current_control:
                    if current_control["paused"] or current_control["killed"]:
                        telegram = deliver_control(
                            state, control=current_control, wake_id=wake_id, mode=mode)
                        print(json.dumps({
                            "effect": "none", "loop_id": "alpaca-investment", "mode": mode,
                            "reconciliation": reconciliation,
                            "status": "killed" if current_control["killed"] else "paused",
                            "telegram_message_id": telegram["message_id"],
                        }, separators=(",", ":")))
                        return 0
                    sealed = seal(state / "receipts.jsonl", exit_decision, order)
                    mark_started(state / "receipts.jsonl", sealed)
                    effect_attempted = True
                    submit_order(credentials_path=credentials_path, cli_path=cli_path,
                                 client_order_id=sealed["client_order_id"], order=order)
                stage = "campaign_exit_reconcile"
                reconcile_started(
                    state / "receipts.jsonl",
                    lambda value: find_order_by_client_id(
                        credentials_path=credentials_path, cli_path=cli_path, client_order_id=value),
                )
                effect = sealed["effect_id"]
                stage = "campaign_exit_observe"
                observation = observe(credentials_path=credentials_path, cli_path=cli_path)
                stage = "campaign_exit_campaign_read"
                campaign = reconcile(read_campaign_snapshot(
                    credentials_path=credentials_path, cli_path=cli_path, symbols=SYMBOLS))
        stage = "allocator_read"
        allocator_snapshot = read_allocator_snapshot(
            credentials_path=credentials_path, cli_path=cli_path,
            risk_day_path=state / "risk-day.json")
        unresolved = reconciliation.get("unresolved")
        if isinstance(unresolved, bool) or not isinstance(unresolved, int) or unresolved != 0:
            raise ValueError("investment_unresolved_intent")
        allocator_snapshot["unresolved_intents"] = unresolved
        candidates = build_candidates(allocator_snapshot)
        stage = "allocation_decide"
        runner = Path(__file__).resolve().parents[2] / "runtime/agent-runner/agent_runner.py"
        workdir = Path(__file__).resolve().parents[2]
        live_positions = mode == "live" and allocator_snapshot.get("positions", 0) > 0
        if live_positions:
            _owned_live_position(state, credentials_path, cli_path, observation)
            position = choose_position(allocator_snapshot, observation, state, runner, workdir)
            decision = {"approved": position["action"] == "EXIT",
                        "candidate_ref": "position://BTCUSD", "gate": "position_exit" if position["action"] == "EXIT" else "position_hold",
                        "reason": position["reason"], "position_action": position["action"],
                        "position_qty": position["qty"], "observed_at": allocator_snapshot["clock"]["timestamp"]}
        else:
            decision = choose(allocator_snapshot, candidates, state, runner, workdir)
        decision["deployment"] = deployment
        decision["mode"] = mode
        decision["risk"] = allocator_snapshot["risk"]
        if effect != "none" and decision["approved"]:
            decision["approved"] = False
            decision["gate"] = "campaign_exit_used_effect_limit"
        if decision["approved"] and mode in {"paper", "live"}:
            stage = "allocation_order_build"
            order = (live_exit_order({"qty": decision["position_qty"]})
                     if live_positions else order_for(decision))
            if mode == "live" and not live_positions:
                if order.get("asset_class") != "crypto" or order.get("symbol") != "BTC/USDC":
                    decision.update({"approved": False, "gate": "live_asset_rejected"})
                    record_no_trade(state / "receipts.jsonl", decision)
                    order = None
            if order is None:
                effect = "none"
            else:
                stage = "allocation_submit"
                with control_fence(state) as current_control:
                    if current_control["paused"] or current_control["killed"]:
                        telegram = deliver_control(state, control=current_control, wake_id=wake_id, mode=mode)
                        print(json.dumps({"effect":"none","loop_id":"alpaca-investment","mode":mode,
                            "reconciliation":reconciliation,"status":"killed" if current_control["killed"] else "paused",
                            "telegram_message_id":telegram["message_id"]}, separators=(",", ":")))
                        return 0
                    # Re-read official slots under the exclusive effect fence so two
                    # overlapping wakes cannot both act on the same stale snapshot.
                    fresh = read_allocator_snapshot(credentials_path=credentials_path,
                        cli_path=cli_path, risk_day_path=state / "risk-day.json")
                    fresh["unresolved_intents"] = unresolved_intent_count(state / "receipts.jsonl")
                    if (fresh.get("open_orders") != 0 or unresolved_intent_count(
                            state / "receipts.jsonl") != 0 or
                            (live_positions and fresh.get("positions") != 1) or
                            (not live_positions and fresh.get("positions") != 0)):
                        raise ValueError("investment_effect_fence_rejected")
                    if mode == "live" and not live_positions and not evaluate_entry(
                            fresh.get("risk"), order.get("notional_usd"))["approved"]:
                        raise ValueError("investment_effect_fence_rejected")
                    if mode == "live" and not live_positions:
                        refreshed = allocation_gate(fresh, build_candidates(fresh), decision)
                        if not refreshed["approved"]:
                            raise ValueError("investment_effect_fence_rejected")
                    sealed = seal(state / "receipts.jsonl", decision, order)
                    if not mark_started(state / "receipts.jsonl", sealed):
                        raise ValueError("investment_effect_already_started")
                    if mode == "live":
                        _atomic_json(state / "live-owned-position.json", {
                            "client_order_id": sealed["client_order_id"], "effect_id": sealed["effect_id"],
                            "status": "closing" if live_positions else "open", "symbol": "BTCUSD"})
                    effect_attempted = True
                    acknowledgement = submit_order(credentials_path=credentials_path, cli_path=cli_path,
                        client_order_id=sealed["client_order_id"], order=order, mode=mode)
                stage = "allocation_reconcile"
                reconcile_started(state / "receipts.jsonl", lambda value: find_order_by_client_id(
                    credentials_path=credentials_path, cli_path=cli_path, client_order_id=value))
                effect = sealed["effect_id"]
        else:
            if decision["approved"]:
                decision.update({"approved": False, "gate": f"{mode}_read_only"})
            record_no_trade(state / "receipts.jsonl", decision)
        review = _review_status(state, mode, deployment)
        decision["application_status"] = review.get("application_status", "unknown")
        stage = "state_write"
        _atomic_json(state / "allocation-latest.json", decision)
        _atomic_json(state / "risk-latest.json", allocator_snapshot["risk"])
        _atomic_json(state / "observation-latest.json", observation)
        _atomic_json(state / "campaign.json", campaign)
        stage = "telegram_deliver"
        telegram = deliver(state, observation, campaign, decision, effect)
        summary = {
            "account": observation["account"],
            "activities_count": observation["activities_count"],
            "candidate_count": len(candidates),
            "decision": decision["candidate_ref"],
            "deployment": deployment,
            "effect": effect,
            "exit_status": campaign["exit_status"],
            "loop_id": "alpaca-investment",
            "orders_count": observation["open_and_closed_orders_count"],
            "mode": mode,
            "paper": mode == "paper",
            "positions_count": len(observation["positions"]),
            "unrealized_pnl_usd": campaign["unrealized_pnl_usd"],
            "reconciliation": reconciliation,
            "status": "allocated",
            "telegram_message_id": telegram["message_id"],
        }
        print(json.dumps(summary, separators=(",", ":")))
        return 0
    except Exception as error:
        # Read/agent/report failures before an effect are transient-safe to retry. Once submit_order
        # was called, never retry: an unknown broker acknowledgement must reconcile on the next wake.
        if _retry_allowed(stage, effect_attempted, attempt):
            return main(attempt=attempt + 1, wake_id=wake_id)
        telegram = {"status": "delivery_uncertain"}
        if stage != "telegram_deliver":
            try:
                telegram = deliver_failure(
                    state,
                    stage=stage,
                    effect_uncertain=effect_attempted or stage == "reconcile_started",
                    wake_id=wake_id,
                    observation=observation,
                    campaign=campaign,
                    mode=mode if mode in {"paper", "shadow", "live"} else "unknown",
                )
            except Exception:
                pass
        print(json.dumps({
            "blocker": "alpaca_pass_failed",
            "effect": _terminal_effect(effect_attempted),
            "error_code": _error_code(error),
            "loop_id": "alpaca-investment",
            "mode": mode if mode in {"paper", "shadow", "live"} else "unknown",
            "stage": stage,
            "status": "blocked",
            "telegram_status": telegram["status"],
        }, separators=(",", ":")))
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
