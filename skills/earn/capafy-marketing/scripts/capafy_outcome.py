#!/usr/bin/env python3
"""Validate and render truthful Capafy outcome envelopes.

This module is deliberately pure: it reads one JSON object, validates or
renders it, and never performs network, Telegram, or runtime-state I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


PLACEHOLDER = re.compile(r"\{[^{}]+\}")
MONEY_FIELDS = (
    "gross_usd",
    "pending_usd",
    "realized_usd",
    "mrr_usd",
    "cost_usd",
    "contribution_usd",
)
INCIDENT_PHASES = {
    "detected": {"repair_started", "unresolved"},
    "repair_started": {"repaired", "unresolved"},
    "repaired": {"verified", "unresolved"},
    "unresolved": {"repair_started"},
    "verified": set(),
}


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _is_https_url(value: Any, *, host_suffix: str | None = None) -> bool:
    if not isinstance(value, str) or PLACEHOLDER.search(value):
        return False
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    if host_suffix is None:
        return True
    host = (parsed.hostname or "").lower()
    return host == host_suffix or host.endswith(f".{host_suffix}")


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _normalized_timestamp(value: Any) -> str | None:
    if not _is_timestamp(value):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def validate_outcome(data: dict) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")

    if any(PLACEHOLDER.search(value) for value in _strings(data)):
        errors.append("literal placeholder tokens are not deliverable")

    kind = data.get("kind")
    if kind in {"builder_submitted", "repair_closure"}:
        if not _is_https_url(data.get("listing_url"), host_suffix="capafy.ai"):
            errors.append("listing_url must be a real https://capafy.ai URL")
        if not data.get("agent_id"):
            errors.append("agent_id is required")
        if data.get("remote_status") not in {1, 4}:
            errors.append("remote_status must be a verified submitted or public state")
        if data.get("skills_confirmed") is not True:
            errors.append("skills_confirmed must be true")
        if data.get("config_confirmed") is not True:
            errors.append("config_confirmed must be true")
        for field in MONEY_FIELDS:
            if field not in data:
                errors.append(f"{field} is required and must remain separate")
            else:
                try:
                    Decimal(str(data[field]))
                except (InvalidOperation, TypeError, ValueError):
                    errors.append(f"{field} must be numeric")
    elif kind == "marketing_published":
        for field in ("reel_url", "listing_url", "campaign_url"):
            if not _is_https_url(data.get(field)):
                errors.append(f"{field} must be a real HTTPS URL")
        if not data.get("caption"):
            errors.append("caption is required")
        if not data.get("media_path"):
            errors.append("media_path is required")
        if data.get("owner_session_verified") is not True:
            errors.append("owner_session_verified must be true after publishing")
    elif kind == "marketing_dry":
        if not _is_https_url(data.get("listing_url"), host_suffix="capafy.ai"):
            errors.append("listing_url must be a real https://capafy.ai URL")
        for field in ("title", "agent_id", "caption", "media_path"):
            if not data.get(field):
                errors.append(f"{field} is required")
    elif kind == "account_state":
        if not data.get("handle"):
            errors.append("handle is required")
        if not data.get("lifecycle_status"):
            errors.append("lifecycle_status is required")
        if not data.get("capability"):
            errors.append("capability is required")
        public_url = data.get("public_post_url")
        if public_url is not None and not _is_https_url(public_url):
            errors.append("public_post_url must be a real HTTPS URL")
    elif kind == "account_created":
        if not data.get("handle"):
            errors.append("handle is required")
        if data.get("session_owner") != "browser":
            errors.append("session_owner must be browser")
        if data.get("session_established") is not True:
            errors.append("session_established must be true")
        if data.get("capability") != "publish_probe":
            errors.append("capability must be publish_probe for a verified fresh account")
        if data.get("public_post_url") is not None:
            errors.append("a fresh account cannot have a public_post_url")
        if not data.get("next_action"):
            errors.append("next_action is required")
    elif kind == "incident_unresolved":
        for field in (
            "incident_id",
            "owner",
            "detected_summary",
            "repair_summary",
            "blocker",
            "next_retry_at",
        ):
            if not data.get(field):
                errors.append(f"{field} is required")
    elif kind == "builder_noop":
        if not data.get("reason"):
            errors.append("reason is required")
        for field in MONEY_FIELDS:
            if field not in data:
                errors.append(f"{field} is required and must remain separate")
            else:
                try:
                    Decimal(str(data[field]))
                except (InvalidOperation, TypeError, ValueError):
                    errors.append(f"{field} must be numeric")
    elif kind == "company_state":
        inventory = data.get("inventory")
        account = data.get("account")
        marketing = data.get("marketing")
        if not isinstance(inventory, dict):
            errors.append("inventory is required")
        else:
            for field in ("online", "under_review", "draft", "rejected"):
                if not isinstance(inventory.get(field), int):
                    errors.append(f"inventory.{field} must be an integer")
        if not isinstance(account, dict) or not account.get("handle"):
            errors.append("account.handle is required")
        if not isinstance(marketing, dict):
            errors.append("marketing is required")
        else:
            for field in ("public_post_url", "campaign_url"):
                if marketing.get(field) is not None and not _is_https_url(
                    marketing[field]
                ):
                    errors.append(f"marketing.{field} must be a real HTTPS URL")
        metrics = data.get("metrics")
        if not isinstance(metrics, dict) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in metrics.values()
        ):
            errors.append("metrics must contain non-negative integer values")
        orders = data.get("orders")
        if "orders" not in data or isinstance(orders, bool) or not isinstance(orders, int) or orders < 0:
            errors.append("orders must be a non-negative integer")
        if "paid_orders" not in data:
            errors.append("paid_orders is required")
        elif data["paid_orders"] is not None and (
            isinstance(data["paid_orders"], bool)
            or not isinstance(data["paid_orders"], int)
            or data["paid_orders"] < 0
        ):
            errors.append("paid_orders must be a non-negative integer or null")
        elif data["paid_orders"] is not None and isinstance(orders, int) and data["paid_orders"] > orders:
            errors.append("paid_orders must not exceed orders")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(data.get("projection_id") or "")):
            errors.append("projection_id must be sha256:<64 lowercase hex>")
        if not data.get("last_event_id"):
            errors.append("last_event_id is required")
        try:
            parsed_as_of = datetime.fromisoformat(
                str(data.get("as_of") or "").replace("Z", "+00:00")
            )
            if parsed_as_of.utcoffset() is None:
                raise ValueError
        except ValueError:
            errors.append("as_of must be an RFC3339 timestamp")
        for field in MONEY_FIELDS:
            if field not in data:
                errors.append(f"{field} is required and must remain separate")
        if not _is_https_url(data.get("dashboard_url")):
            errors.append("dashboard_url must be a real HTTPS URL")
        if data.get("listing_url") and not _is_https_url(
            data.get("listing_url"), host_suffix="capafy.ai"
        ):
            errors.append("listing_url must be a real https://capafy.ai URL")
        experiment = data.get("experiment")
        if experiment is not None:
            if not isinstance(experiment, dict) or not experiment.get("experiment_id"):
                errors.append("experiment must be null or a structured active experiment")
            elif experiment.get("public_url") and not _is_https_url(
                experiment["public_url"], host_suffix="capafy.ai"
            ):
                errors.append("experiment.public_url must be a real https://capafy.ai URL")
    else:
        errors.append(f"unsupported kind: {kind!r}")
    return errors


def _money(value: Any) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):.2f}"


def _money_lines(data: dict) -> list[str]:
    return [
        f"Lifetime gross: {_money(data['gross_usd'])}",
        f"Pending seller balance: {_money(data['pending_usd'])}",
        f"Realized bank payout: {_money(data['realized_usd'])}",
        f"MRR: {_money(data['mrr_usd'])}",
        f"Model/tool cost: {_money(data['cost_usd'])}",
        f"Contribution after recorded cost: {_money(data['contribution_usd'])}",
    ]


def _verified_state(data: dict) -> str:
    return (
        f"Verified remote state: status {data['remote_status']}; "
        "skill/config confirmed"
    )


def render_outcome(data: dict) -> str:
    errors = validate_outcome(data)
    if errors:
        raise ValueError("; ".join(errors))

    kind = data["kind"]
    if kind == "account_created":
        return "\n".join(
            [
                "Capafy Marketer — replacement account created and verified",
                f"Account: @{data['handle']}",
                "The isolated browser session is established and independently verified.",
                "Publish probe capability is verified; no elapsed-day gate applies.",
                "No public post exists yet.",
                f"The first original Reel starts now. Next automatic action: {data['next_action']}.",
            ]
        )
    if kind == "account_state":
        schedule = (
            "The scheduler is loaded."
            if data.get("scheduler_loaded")
            else "The scheduler is not loaded."
        )
        session = (
            "The posting session is established."
            if data.get("session_established")
            else "The posting session is not established."
        )
        post = (
            f"Verified public post: {data['public_post_url']}"
            if data.get("public_post_url")
            else "No public post is verified."
        )
        return "\n".join(
            [
                f"Capafy Instagram account @{data['handle']}",
                f"Lifecycle: {data['lifecycle_status']}; capability: {data['capability'].replace('_', '-')}.",
                schedule,
                session,
                post,
            ]
        )

    if kind == "repair_closure":
        lines = [
            "Capafy incident resolved — no action needed",
            data.get("detected_summary", "A Capafy operation failed."),
            data.get("repair_summary", "The repair owner restored the operation."),
            f"Recovered skill: {data['title']} ({data['agent_id']})",
            _verified_state(data),
            f"Evidence: {data['listing_url']}",
            *_money_lines(data),
            f"Next: {data['next_action']}",
        ]
        return "\n".join(lines)

    if kind == "incident_unresolved":
        return "\n".join(
            [
                "Capafy incident remains unresolved — automatic retry scheduled",
                f"Detected: {data['detected_summary']}",
                f"Repair attempted: {data['repair_summary']}",
                f"Remaining blocker: {data['blocker']}",
                f"Next automatic retry: {data['next_retry_at']}",
                "Human action required: none.",
            ]
        )

    if kind == "builder_noop":
        return "\n".join(
            [
                "Capafy Builder — completed without a new submission",
                f"Reason: {data['reason']}",
                *_money_lines(data),
                "Next: retry the bounded Builder objective on the next scheduled pass.",
            ]
        )

    if kind == "marketing_dry":
        return "\n".join(
            [
                "Capafy Marketer — DRY creative — not posted",
                f"Skill: {data['title']} ({data['agent_id']})",
                f"Open the skill: {data['listing_url']}",
                f"Media artifact: {data['media_path']}",
                f"Caption: {data['caption']}",
                "Current result: creative verified locally; no public post exists.",
            ]
        )

    if kind == "company_state":
        inventory = data["inventory"]
        account = data["account"]
        marketing = data["marketing"]
        orders = int(data.get("orders") or 0)
        order_word = "order" if orders == 1 else "orders"
        paid_text = (
            f"{data['paid_orders']} paid"
            if data["paid_orders"] is not None
            else "paid count unavailable"
        )
        session = (
            "The posting session is established."
            if account.get("session_established")
            else "The posting session is not established."
        )
        if marketing.get("public_post_url"):
            marketing_line = f"Verified public post: {marketing['public_post_url']}"
        elif marketing.get("scheduler_loaded"):
            marketing_line = "Marketing is scheduled; no public post is verified."
        else:
            marketing_line = "Marketing is not scheduled; no public post is verified."
        lines = [
            f"Capafy — Consolidated company state, {data['date']}",
            f"Projection: {data['projection_id'].removeprefix('sha256:')[:12]}",
            (
                f"Products: {inventory['online']} online, "
                f"{inventory['under_review']} under review, {inventory['draft']} "
                f"{'draft' if inventory['draft'] == 1 else 'drafts'}, "
                f"{inventory['rejected']} rejected."
            ),
            f"Sales: {orders} lifetime {order_word} / {paid_text} / {_money(data['gross_usd'])} gross.",
            *_money_lines(data)[1:],
            (
                f"Instagram @{account['handle']} — Lifecycle: "
                f"{account.get('lifecycle_status', 'unknown')}; capability: "
                f"{str(account.get('capability', 'none')).replace('_', '-')}. {session} "
                f"Account status: {account.get('account_status', 'unknown')}."
            ),
            marketing_line,
        ]
        metrics = data.get("metrics") or {}
        metric_labels = {
            "views": "Views",
            "likes": "Likes",
            "comments": "Comments",
            "clicks": "Attributed clicks",
        }
        measured = [
            f"{metric_labels[field]}: {metrics[field]}"
            for field in ("views", "likes", "comments", "clicks")
            if field in metrics
        ]
        if measured:
            lines.append("Marketing measurements — " + "; ".join(measured) + ".")
        if marketing.get("campaign_url"):
            lines.append(f"Attributed campaign: {marketing['campaign_url']}")
        incident = data.get("incident")
        if isinstance(incident, dict):
            lines.append(
                "Open incident: "
                f"{incident.get('summary', 'unspecified')} "
                f"[{incident.get('phase', 'unknown')}]; next retry: "
                f"{incident.get('next_retry_at') or 'automatic retry pending'}."
            )
        if data.get("listing_url"):
            lines.append(f"Latest Builder evidence: {data['listing_url']}")
        experiment = data.get("experiment")
        if isinstance(experiment, dict):
            observed = experiment.get("observed_contribution_usd")
            observed_text = _money(observed) if observed is not None else "not measured"
            experiment_label = (
                "Active revenue experiment"
                if experiment.get("status") == "active"
                else "Stopped revenue experiment"
            )
            lines.extend(
                [
                    (
                        f"{experiment_label}: {experiment.get('purchase_model')} at "
                        f"{_money(experiment.get('price_usd') or 0)} on product "
                        f"{experiment.get('agent_id')}."
                    ),
                    (
                        f"Projected contribution: {_money(experiment.get('projected_contribution_usd') or 0)} "
                        f"(not realized). Observed contribution: {observed_text}."
                    ),
                    f"Experiment link: {experiment.get('public_url')}",
                    f"Success: {experiment.get('success_metric') or 'not specified'}",
                    f"Stop: {experiment.get('stop_condition') or 'not specified'}",
                ]
            )
            if experiment.get("stop_reason"):
                lines.append(f"Stop reason: {experiment['stop_reason']}")
        lines.append(f"Dashboard: {data['dashboard_url']}")
        return "\n".join(lines)

    if kind == "builder_submitted":
        return "\n".join(
            [
                "Capafy Builder — New skill submitted and verified",
                f"Skill: {data['title']} ({data['agent_id']})",
                _verified_state(data),
                f"Open the real Capafy page: {data['listing_url']}",
                *_money_lines(data),
                f"Next: {data['next_action']}",
            ]
        )

    return "\n".join(
        [
            "Capafy Marketer — Reel published and verified",
            f"Skill: {data['title']}",
            f"Watch the Reel: {data['reel_url']}",
            f"Open the skill: {data['listing_url']}",
            f"Campaign link: {data['campaign_url']}",
            f"Media artifact: {data['media_path']}",
            f"Caption: {data['caption']}",
            "The browser owner session was re-verified after publishing.",
        ]
    )


def delivery_key(data: dict) -> str:
    canonical = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _state_root() -> Path:
    configured = os.environ.get("CAPAFY_OUTCOME_STATE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".local/state/life-manager/state"


def _incident_dir() -> Path:
    return _state_root() / "capafy-incidents"


def _write_incident_event(record: dict) -> None:
    """Append the persisted incident phase unless explicitly disabled for tests."""

    if os.environ.get("CAPAFY_EVENT_WRITE_DISABLED") == "1":
        return
    from capafy_event_adapters import event_from_incident
    from capafy_event_store import append_event

    state_root = _state_root()
    ledger = Path(
        os.environ.get("CAPAFY_EVENT_LEDGER", state_root / "capafy-revenue-events.jsonl")
    ).expanduser()
    evidence_dir = Path(
        os.environ.get(
            "CAPAFY_EVENT_EVIDENCE_DIR", state_root / "capafy-revenue-evidence"
        )
    ).expanduser()
    append_event(ledger, event_from_incident(record), record, evidence_dir)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_incidents() -> list[dict]:
    records = []
    for path in _incident_dir().glob("*.json") if _incident_dir().exists() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def start_incident(
    *,
    owner: str,
    summary: str,
    fingerprint: str,
    repair_result_path: str | None,
    event_writer: Callable[[dict], None] | None = None,
) -> dict:
    for record in sorted(
        _read_incidents(), key=lambda item: item.get("detected_at", ""), reverse=True
    ):
        if (
            record.get("owner") == owner
            and record.get("fingerprint") == fingerprint
            and record.get("phase") != "verified"
        ):
            phase = record.get("phase", "detected")
            phase_timestamps = record.setdefault("phase_timestamps", {})
            changed = False
            if "detected" not in phase_timestamps:
                phase_timestamps["detected"] = record.get("detected_at") or _now()
                changed = True
            if phase not in phase_timestamps:
                phase_timestamps[phase] = record.get("updated_at") or _now()
                changed = True
            if changed:
                _atomic_json_write(
                    _incident_dir() / f"{record['incident_id']}.json", record
                )
            if event_writer is not None:
                event_writer(record)
            return record

    now = _now()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    incident_id = f"capafy-{owner}-{timestamp}-{secrets.token_hex(4)}"
    record = {
        "schema_version": 1,
        "incident_id": incident_id,
        "owner": owner,
        "phase": "detected",
        "detected_at": now,
        "updated_at": now,
        "phase_timestamps": {"detected": now},
        "summary": summary,
        "fingerprint": fingerprint,
        "repair_result_path": repair_result_path,
        "attempts": 0,
        "next_retry_at": None,
        "terminal_message_key": None,
    }
    _atomic_json_write(_incident_dir() / f"{incident_id}.json", record)
    if event_writer is not None:
        event_writer(record)
    return record


def transition_incident(
    update: dict, event_writer: Callable[[dict], None] | None = None
) -> dict:
    incident_id = update.get("incident_id")
    phase = update.get("phase")
    if not isinstance(incident_id, str) or not incident_id:
        raise ValueError("incident_id is required")
    if phase not in INCIDENT_PHASES:
        raise ValueError(f"unsupported incident phase: {phase!r}")
    path = _incident_dir() / f"{incident_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"incident not found: {incident_id}") from exc
    current = record.get("phase")
    if phase != current and phase not in INCIDENT_PHASES.get(current, set()):
        raise ValueError(f"incident phase cannot move backwards: {current} -> {phase}")
    if current == "unresolved" and phase == "repair_started":
        record["attempts"] = int(record.get("attempts") or 0) + 1
    now = _now()
    new_occurrence = phase != current
    if phase == current:
        for field in ("summary", "repair_summary", "verification", "outcome"):
            if field in update and update[field] != record.get(field):
                new_occurrence = True
    # Retry scheduling is a machine contract. Older callers supplied prose
    # such as "the next repair cycle", which made the health watchdog treat a
    # real retry as missing forever. Preserve the incident but repair that
    # boundary to a concrete, near-term retry timestamp.
    if phase == "unresolved":
        previous_retry_at = _normalized_timestamp(record.get("next_retry_at"))
        retry_at = update.get("next_retry_at", record.get("next_retry_at"))
        normalized_retry_at = _normalized_timestamp(retry_at)
        if normalized_retry_at is None and current == "unresolved":
            normalized_retry_at = previous_retry_at
        if normalized_retry_at is None:
            retry_dt = datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(hours=1)
            normalized_retry_at = retry_dt.isoformat(timespec="seconds").replace(
                "+00:00", "Z"
            )
        update["next_retry_at"] = normalized_retry_at
        if normalized_retry_at != previous_retry_at:
            new_occurrence = True
    for field in (
        "phase",
        "summary",
        "repair_summary",
        "verification",
        "outcome",
        "next_retry_at",
        "terminal_message_key",
        "telegram_message_id",
    ):
        if field in update:
            record[field] = update[field]
    phase_timestamps = record.setdefault("phase_timestamps", {})
    phase_timestamps.setdefault("detected", record.get("detected_at") or now)
    phase_occurrences = record.setdefault("phase_occurrences", {})
    if new_occurrence:
        try:
            occurrence = int(phase_occurrences.get(phase) or 0)
        except (TypeError, ValueError):
            occurrence = 0
        if occurrence == 0 and phase in phase_timestamps:
            occurrence = 1
        phase_occurrences[phase] = occurrence + 1
        phase_timestamps[phase] = now
    else:
        phase_timestamps.setdefault(phase, now)
    record["updated_at"] = now
    _atomic_json_write(path, record)
    if event_writer is not None:
        event_writer(record)
    return record


def get_active_incident(owner: str) -> dict:
    matches = [
        record
        for record in _read_incidents()
        if record.get("owner") == owner and record.get("phase") != "verified"
    ]
    if not matches:
        raise ValueError(f"no active incident for owner: {owner}")
    return max(matches, key=lambda item: item.get("updated_at", ""))


def get_incident(incident_id: str) -> dict:
    path = _incident_dir() / f"{incident_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"incident not found: {incident_id}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"incident is not a JSON object: {incident_id}")
    return value


def load_json_stdin() -> dict:
    value = json.load(sys.stdin)
    if not isinstance(value, dict):
        raise ValueError("outcome must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: capafy_outcome.py <command>", file=sys.stderr)
        return 2
    try:
        command = args[0]
        if command == "start-incident":
            def option(name: str, *, required: bool = True) -> str | None:
                try:
                    return args[args.index(name) + 1]
                except (ValueError, IndexError):
                    if required:
                        raise ValueError(f"{name} is required")
                    return None

            record = start_incident(
                owner=option("--owner") or "",
                summary=option("--summary") or "",
                fingerprint=option("--fingerprint") or "",
                repair_result_path=option("--repair-result-path", required=False),
                event_writer=_write_incident_event,
            )
            print(json.dumps(record, ensure_ascii=False, sort_keys=True))
            return 0
        if command == "get-active-incident":
            try:
                owner = args[args.index("--owner") + 1]
            except (ValueError, IndexError) as exc:
                raise ValueError("--owner is required") from exc
            print(json.dumps(get_active_incident(owner), ensure_ascii=False, sort_keys=True))
            return 0
        if command == "get-incident":
            try:
                incident_id = args[args.index("--incident-id") + 1]
            except (ValueError, IndexError) as exc:
                raise ValueError("--incident-id is required") from exc
            print(json.dumps(get_incident(incident_id), ensure_ascii=False, sort_keys=True))
            return 0
        if command == "transition-incident":
            print(
                json.dumps(
                    transition_incident(
                        load_json_stdin(), event_writer=_write_incident_event
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if len(args) != 1 or command not in {"validate", "render", "delivery-key"}:
            raise ValueError(f"unsupported command: {command}")
        data = load_json_stdin()
        if command == "validate":
            errors = validate_outcome(data)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 2
            print(json.dumps({"valid": True}))
        elif command == "render":
            print(render_outcome(data))
        else:
            errors = validate_outcome(data)
            if errors:
                raise ValueError("; ".join(errors))
            print(delivery_key(data))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
