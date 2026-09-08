#!/usr/bin/env python3
"""Generate Writer money UI and deliver durable, receipt-backed Telegram reports."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from writer_report import JST, build_snapshot, render_html, render_message  # noqa: E402
from writer_runtime_paths import life_manager_env_file  # noqa: E402


Transport = Callable[[str], str]
MAX_TELEGRAM_CHARS = 4096
SAFE_CHUNK_CHARS = 3800
SEMANTIC_SCHEMA_VERSION = 4
REPORT_TEXT_SCHEMA_VERSION = 2


def _atomic(path: Path, value: dict[str, Any] | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n"
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _semantic_hash(snapshot: dict[str, Any]) -> str:
    money = snapshot["money"]
    visible_period_keys = (
        "verified_revenue_event_count",
        "verified_gross_by_currency",
        "verified_net_by_currency",
        "verified_gross_by_stream",
        "verified_refunds_by_currency",
        "verified_fees_by_currency",
        "paid_out_by_currency",
    )
    visible_money = {
        period: {key: money[period].get(key) for key in visible_period_keys}
        for period in ("today", "month", "week", "previous_week")
    }
    visible_money.update({
        "mrr": money.get("mrr"),
        "available_balance": {
            key: money.get("available_balance", {}).get(key)
            for key in ("status", "value", "unit", "reason")
        },
        "pending_payout": {
            key: money.get("pending_payout", {}).get(key)
            for key in ("status", "by_currency", "reason")
        },
        "payout_receipts": money.get("payout_receipts", []),
    })
    visible_articles = [{
        "artifact_id": article.get("artifact_id"),
        "platform": article.get("platform"),
        "title": article.get("title"),
        "live_url": article.get("live_url"),
        "revenue_capable": article.get("revenue_capable"),
        "visible_metrics": {
            metric: {
                key: article.get("metrics", {}).get(metric, {}).get(key)
                for key in ("value", "unit", "status", "reason")
            }
            for metric in ("views", "price", "paywall_active", "paid_post_active")
        },
        "money": article.get("money"),
    } for article in snapshot["articles"]]
    stable = {
        "report_text_schema_version": REPORT_TEXT_SCHEMA_VERSION,
        "report_articles_scope": snapshot.get("report_articles_scope"),
        "money": visible_money,
        "articles": visible_articles,
        "opportunities": [{
            key: item.get(key) for key in ("publisher", "state", "next_action")
        } for item in snapshot["opportunities"].get("active", [])],
        "commercial": snapshot.get("commercial"),
        "incident_timeline": snapshot.get("incident_timeline"),
        "learning": {
            "day_diff": {
                key: snapshot.get("learning", {}).get("day_diff", {}).get(key)
                for key in (
                    "status", "causal", "from_run_id", "to_run_id", "deltas", "reason"
                )
            },
            "latest_experiment": (
                {
                    key: snapshot.get("learning", {})
                    .get("latest_experiment", {})
                    .get(key)
                    for key in (
                        "experiment_id", "decision", "changed_field", "text_diff",
                        "reason", "canary_deltas", "rollback_strategy_sha256",
                        "consumed_by_run_id",
                    )
                }
                if isinstance(
                    snapshot.get("learning", {}).get("latest_experiment"), dict
                )
                else None
            ),
        },
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def chunk_message(message: str, limit: int = SAFE_CHUNK_CHARS) -> list[str]:
    if limit > MAX_TELEGRAM_CHARS:
        raise ValueError("Telegram chunk limit exceeds provider maximum")
    chunks: list[str] = []
    current = ""
    for original_line in message.splitlines():
        lines = [original_line[index:index + limit] for index in range(0, len(original_line), limit)] or [""]
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                chunks.append(current)
                current = line
    if current or not chunks:
        chunks.append(current)
    return chunks


def _env_value(path: Path, key: str) -> str:
    process_value = os.environ.get(key, "").strip()
    if process_value:
        return process_value
    try:
        lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        if name.strip() != key:
            continue
        value = raw_value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        return value.strip()
    return ""


def telegram_api_transport(
    target: str, *, env_file: Path | None = None,
) -> Transport:
    if not target:
        raise ValueError("Telegram target is required")
    configured_env = env_file or life_manager_env_file()
    token = _env_value(configured_env, "LM_TELEGRAM_BOT_TOKEN")
    if not token:
        token = _env_value(configured_env, "TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Telegram Bot API token is unavailable")
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"

    def resolve_ipv4() -> str:
        for command in (
            ("/usr/bin/dig", "+short", "@1.1.1.1", "api.telegram.org", "A"),
            ("/usr/bin/nslookup", "-type=A", "api.telegram.org"),
        ):
            try:
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=5, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            values = re.findall(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", result.stdout)
            for value in values:
                try:
                    ipaddress.ip_address(value)
                except ValueError:
                    continue
                return value
        raise RuntimeError("Telegram DNS fallback could not resolve api.telegram.org")

    def curl_resolve_send(body: bytes) -> str:
        ip = resolve_ipv4()
        config = "\n".join((
            "silent",
            "show-error",
            "fail-with-body",
            "max-time = 15",
            'request = "POST"',
            f'resolve = "api.telegram.org:443:{ip}"',
            f"url = {json.dumps(endpoint)}",
            'header = "Content-Type: application/x-www-form-urlencoded"',
            f"data = {json.dumps(body.decode('utf-8'))}",
            "",
        ))
        try:
            result = subprocess.run(
                ("/usr/bin/curl", "--config", "-"),
                input=config,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("Telegram Bot API transport failed")
            value = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            raise RuntimeError("Telegram Bot API transport failed") from None
        message_id = str(value.get("result", {}).get("message_id", "")).strip()
        if value.get("ok") is not True or not message_id:
            raise RuntimeError("Telegram Bot API transport failed")
        return message_id

    def is_dns_failure(error: BaseException) -> bool:
        reason = getattr(error, "reason", error)
        if not isinstance(reason, socket.gaierror):
            return False
        dns_errnos = {
            value
            for value in (
                getattr(socket, "EAI_AGAIN", None),
                getattr(socket, "EAI_NONAME", None),
                getattr(socket, "EAI_NODATA", None),
            )
            if value is not None
        }
        return reason.errno in dns_errnos

    def send(message: str) -> str:
        body = urllib.parse.urlencode({"chat_id": target, "text": message}).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                value = json.loads(response.read().decode("utf-8"))
            message_id = str(value.get("result", {}).get("message_id", "")).strip()
            if value.get("ok") is not True or not message_id:
                raise ValueError("provider returned no message ID")
            return message_id
        except urllib.error.URLError as error:
            if is_dns_failure(error):
                return curl_resolve_send(body)
            raise RuntimeError("Telegram Bot API transport failed") from None
        except Exception:
            # Provider exceptions can contain the token-bearing request URL.
            # Keep both the durable outbox and all logs free of that secret.
            raise RuntimeError("Telegram Bot API transport failed") from None

    return send


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _delivery(
    *, state_path: Path, state: dict[str, Any], cadence: str, window_key: str,
    snapshot: dict[str, Any], transport: Transport,
) -> dict[str, Any]:
    semantic = _semantic_hash(snapshot)
    delivery_id = f"{cadence}:{window_key}:{semantic}"
    existing = next(
        (item for item in state["deliveries"] if item["delivery_id"] == delivery_id), None
    )
    if existing and existing.get("status") == "sent":
        return existing
    if existing is None:
        chunks = chunk_message(render_message(snapshot, cadence=cadence))
        existing = {
            "delivery_id": delivery_id,
            "cadence": cadence,
            "window_key": window_key,
            "semantic_hash": semantic,
            "status": "prepared",
            "chunks": chunks,
            "message_ids": [],
            "prepared_at": datetime.now(JST).isoformat(),
        }
        state["deliveries"].append(existing)
        _atomic(state_path, state)
    return _send_prepared(
        state_path=state_path,
        state=state,
        existing=existing,
        transport=transport,
    )


def _send_prepared(
    *, state_path: Path, state: dict[str, Any], existing: dict[str, Any],
    transport: Transport,
) -> dict[str, Any]:
    chunks = existing.get("chunks")
    message_ids = existing.get("message_ids")
    if (
        not isinstance(chunks, list)
        or not chunks
        or not all(isinstance(chunk, str) and len(chunk) <= MAX_TELEGRAM_CHARS for chunk in chunks)
        or not isinstance(message_ids, list)
        or not all(isinstance(message_id, str) and message_id.strip() for message_id in message_ids)
        or len(message_ids) > len(chunks)
    ):
        raise RuntimeError("invalid durable report outbox")
    for chunk in chunks[len(existing["message_ids"]):]:
        message_id = transport(chunk)
        if not isinstance(message_id, str) or not message_id.strip():
            raise RuntimeError("Telegram transport returned no message ID")
        existing["message_ids"].append(message_id.strip())
        _atomic(state_path, state)
    existing["status"] = "sent"
    existing["sent_at"] = datetime.now(JST).isoformat()
    _atomic(state_path, state)
    return existing


def deliver_snapshot(
    *, snapshot: dict[str, Any], state_dir: Path, transport: Transport, now: datetime,
) -> dict[str, Any]:
    if now.tzinfo is None:
        raise ValueError("worker time must include timezone")
    local_now = now.astimezone(JST)
    reporting = Path(state_dir) / "reporting"
    state_path = reporting / "deliveries.json"
    state = _read_state(state_path)
    semantic = _semantic_hash(snapshot)
    today = local_now.date()
    week_start = today - timedelta(days=today.weekday())
    results: list[dict[str, Any]] = []
    if state is None:
        state = {
            "schema_version": 1,
            "semantic_schema_version": SEMANTIC_SCHEMA_VERSION,
            "initialized_at": local_now.isoformat(),
            "last_semantic_hash": semantic,
            "daily_through": str(today - timedelta(days=1)),
            "weekly_through": str(week_start),
            "last_snapshot": snapshot,
            "deliveries": [],
        }
        _atomic(state_path, state)
        results.append(_delivery(
            state_path=state_path, state=state, cadence="immediate",
            window_key="bootstrap", snapshot=snapshot, transport=transport,
        ))
        return {"deliveries": results, "semantic_hash": semantic}

    if state.get("semantic_schema_version") != SEMANTIC_SCHEMA_VERSION:
        state["semantic_schema_version"] = SEMANTIC_SCHEMA_VERSION
        state["last_semantic_hash"] = semantic
        state["last_snapshot"] = snapshot
        state["last_checked_at"] = local_now.isoformat()
        _atomic(state_path, state)
        return {"deliveries": [], "semantic_hash": semantic, "migrated": True}

    # A prepared outbox is the exact message contract frozen before transport.
    # Resume those bytes first, even when the current semantic snapshot is
    # unchanged or has advanced since the interrupted attempt. Re-rendering a
    # time-bearing report here both loses the durable delivery and creates a
    # false conflict on every launchd retry.
    for prepared in list(state.get("deliveries", [])):
        if prepared.get("status") == "prepared":
            results.append(_send_prepared(
                state_path=state_path,
                state=state,
                existing=prepared,
                transport=transport,
            ))

    previous_snapshot = state.get("last_snapshot") or snapshot
    daily_due = today - timedelta(days=1)
    if str(daily_due) > str(state.get("daily_through", "")):
        results.append(_delivery(
            state_path=state_path, state=state, cadence="daily",
            window_key=str(daily_due), snapshot=previous_snapshot, transport=transport,
        ))
        state["daily_through"] = str(daily_due)
        _atomic(state_path, state)
    previous_week = week_start - timedelta(days=7)
    if str(week_start) > str(state.get("weekly_through", "")):
        results.append(_delivery(
            state_path=state_path, state=state, cadence="weekly",
            window_key=str(previous_week), snapshot=previous_snapshot, transport=transport,
        ))
        state["weekly_through"] = str(week_start)
        _atomic(state_path, state)
    if semantic != state.get("last_semantic_hash"):
        results.append(_delivery(
            state_path=state_path, state=state, cadence="immediate",
            window_key=semantic[:16], snapshot=snapshot, transport=transport,
        ))
    state["last_semantic_hash"] = semantic
    state["last_snapshot"] = snapshot
    state["last_checked_at"] = local_now.isoformat()
    _atomic(state_path, state)
    return {"deliveries": results, "semantic_hash": semantic}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=SCRIPT_DIR.parent / "state")
    parser.add_argument("--target", default=(
        os.environ.get("ARTICLE_TELEGRAM_TARGET")
        or os.environ.get("TELEGRAM_TARGET_ID")
        or "8547730585"
    ))
    parser.add_argument("--fixture-receipt", help=argparse.SUPPRESS)
    parser.add_argument(
        "--telegram-env", type=Path,
        default=life_manager_env_file(), help=argparse.SUPPRESS,
    )
    parser.add_argument("--now")
    args = parser.parse_args(argv)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(JST)
    snapshot = build_snapshot(state_dir=args.state_dir, now=now)
    reporting = args.state_dir / "reporting"
    _atomic(reporting / "latest.json", snapshot)
    _atomic(reporting / "index.html", render_html(snapshot))
    transport = (
        (lambda _message: str(args.fixture_receipt))
        if args.fixture_receipt else telegram_api_transport(
            args.target, env_file=args.telegram_env
        )
    )
    result = deliver_snapshot(
        snapshot=snapshot, state_dir=args.state_dir, transport=transport, now=now
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
