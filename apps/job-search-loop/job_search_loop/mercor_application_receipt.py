"""Persist and immediately report one officially verified Mercor application."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .mercor_pass import _ledger_listing_ids
from .mercor_submit_guard import classify_submit_readback


REPO_ROOT = Path(__file__).resolve().parents[3]
MARKETPLACE_CORE = REPO_ROOT / "skills/_shared/marketplace-core/scripts"
DEFAULT_TELEGRAM_ENV = Path.home() / ".config/anicca/job-search/telegram.env"


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _env_value(path: Path, name: str) -> str:
    supplied = os.environ.get(name, "").strip()
    if supplied:
        return supplied
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        key, separator, value = raw.removeprefix("export ").partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def _private_append(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(path, 0o600)


def record_and_report(
    *,
    state_root: Path,
    outbox_database: Path,
    telegram_env: Path,
    listing_id: str,
    title: str,
    url: str,
    run_id: str,
    readback_evidence: Path,
    evidence_root: Path,
    now: str | None = None,
) -> dict[str, Any]:
    """Record once after official readback, then deliver via the shared outbox."""
    root = evidence_root.expanduser().resolve()
    evidence_path = readback_evidence.expanduser().resolve()
    try:
        evidence_path.relative_to(root)
    except ValueError as error:
        raise ValueError("readback_evidence_outside_current_pass") from error
    value = json.loads(evidence_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("readback_evidence_invalid")
    page_url = value.get("page_url")
    visible_text = value.get("visible_text")
    screenshot = Path(str(value.get("screenshot_path") or "")).expanduser().resolve()
    try:
        screenshot.relative_to(root)
    except ValueError as error:
        raise ValueError("readback_screenshot_outside_current_pass") from error
    if not screenshot.is_file():
        raise ValueError("readback_screenshot_missing")
    if not isinstance(page_url, str) or not isinstance(visible_text, str):
        raise ValueError("readback_evidence_invalid")
    status = classify_submit_readback(page_url=page_url, visible_text=visible_text)
    if status != "submitted_pending_review":
        raise ValueError("official_readback_unverified")

    listing_id = listing_id.strip()
    title = title.strip()
    url = url.strip()
    if not listing_id or not title or not url:
        raise ValueError("application_identity_invalid")
    observed_at = now or datetime.now(timezone.utc).isoformat()
    ledger = state_root.expanduser().resolve() / "applications.jsonl"
    lock_path = ledger.with_name(f"{ledger.name}.lock")
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    recorded = False
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if listing_id not in set(_ledger_listing_ids(ledger)):
            _private_append(ledger, {
                "listing_id": listing_id,
                "title": title,
                "application_url": page_url,
                "source_url": url,
                "status": status,
                "evidence_path": str(evidence_path),
                "run_id": run_id,
                "observed_at": observed_at,
            })
            recorded = True

    notification = _load(
        "mercor_shared_effect_notification", MARKETPLACE_CORE / "effect_notification.py"
    )
    event_key = f"mercor-application:{listing_id}"
    message = (
        "Codex::: Mercorで新しい仕事へ応募しました\n\n"
        f"案件: {title}\n"
        "状態: 応募済み・審査待ち\n"
        "確認: Mercor公式画面で送信完了を確認しました。\n"
        "次: 返信と契約到着をReply loopが確認します。"
    )
    chat_id = _env_value(telegram_env, "JOB_SEARCH_TELEGRAM_CHAT_ID")
    if not chat_id:
        raise RuntimeError("job_search_telegram_chat_unavailable")
    receipt = notification.notify_effect(
        database=outbox_database,
        event_key=event_key,
        message=message,
        observed_at=observed_at,
        chat_id=chat_id,
        env_file=telegram_env,
    )
    return {
        "listing_id": listing_id,
        "recorded": recorded,
        **receipt,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--telegram-env", type=Path, default=DEFAULT_TELEGRAM_ENV)
    parser.add_argument("--listing-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--readback-evidence", required=True, type=Path)
    parser.add_argument("--evidence-root", required=True, type=Path)
    args = parser.parse_args(argv)
    result = record_and_report(
        state_root=args.state_root,
        outbox_database=args.outbox,
        telegram_env=args.telegram_env,
        listing_id=args.listing_id,
        title=args.title,
        url=args.url,
        run_id=args.run_id,
        readback_evidence=args.readback_evidence,
        evidence_root=args.evidence_root,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
