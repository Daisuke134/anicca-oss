"""Record and immediately notify one resumable Mercor human gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .mercor_human_gate import HumanGateStore


REPO_ROOT = Path(__file__).resolve().parents[3]
NOTIFICATION = REPO_ROOT / "skills/_shared/marketplace-core/scripts/effect_notification.py"
DEFAULT_TELEGRAM_ENV = Path.home() / ".config/anicca/job-search/telegram.env"


def _load_notification() -> Any:
    name = "mercor_human_shared_effect_notification"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, NOTIFICATION)
    if spec is None or spec.loader is None:
        raise RuntimeError("shared_effect_notification_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _chat_id(path: Path) -> str:
    value = os.environ.get("JOB_SEARCH_TELEGRAM_CHAT_ID", "").strip()
    if value:
        return value
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for raw in lines:
        key, separator, value = raw.removeprefix("export ").partition("=")
        if separator and key.strip() == "JOB_SEARCH_TELEGRAM_CHAT_ID":
            return value.strip().strip("'\"")
    return ""


def record_and_notify(
    *, gate_store: Path, outbox: Path, telegram_env: Path, run_id: str,
    listing_id: str, title: str, reason: str, evidence_ref: str,
    url: str = "", deadline: str = "公式期限表示なし",
) -> dict[str, Any]:
    gate = HumanGateStore(gate_store).record(
        run_id=run_id, reason=f"{listing_id}: {reason}", evidence_ref=evidence_ref
    )
    chat_id = _chat_id(telegram_env)
    if not chat_id:
        raise RuntimeError("job_search_telegram_chat_unavailable")
    message = (
        "Codex::: Mercor応募に人間操作が必要です\n\n"
        f"案件: {title.strip()}\n"
        f"リンク: {url.strip() or evidence_ref.strip()}\n"
        "アカウント: Mercorの既存Daisukeアカウント\n"
        f"必要な操作: {reason.strip()}\n"
        f"期限: {deadline.strip() or '公式期限表示なし'}\n"
        "状態: 人間操作の直前まで進行済みです。完了後、次のwakeが自動再開します。"
    )
    receipt = _load_notification().notify_effect(
        database=outbox,
        event_key=f"mercor-human-gate:{gate['gate_id']}",
        message=message,
        observed_at=datetime.now(timezone.utc).isoformat(),
        chat_id=chat_id,
        env_file=telegram_env,
    )
    return {"gate_id": gate["gate_id"], **receipt}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-store", required=True, type=Path)
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--telegram-env", type=Path, default=DEFAULT_TELEGRAM_ENV)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--listing-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--url", default="")
    parser.add_argument("--deadline", default="公式期限表示なし")
    parser.add_argument("--evidence-ref", required=True)
    args = parser.parse_args(argv)
    result = record_and_notify(
        gate_store=args.gate_store, outbox=args.outbox, telegram_env=args.telegram_env,
        run_id=args.run_id, listing_id=args.listing_id, title=args.title,
        reason=args.reason, evidence_ref=args.evidence_ref,
        url=args.url, deadline=args.deadline,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
