#!/usr/bin/env python3
"""CFO CLI seam for the shared receipt-backed Telegram outbox."""

import argparse
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "skills/_shared/marketplace-core/scripts/effect_notification.py"


def load_effect_notification():
    spec = importlib.util.spec_from_file_location("cfo_effect_notification", MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError("shared_effect_notification_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--event-key", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    body = json.load(sys.stdin)
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str) or not message:
        raise ValueError("message_required")
    result = load_effect_notification().notify_effect(
        database=Path(args.database),
        event_key=args.event_key,
        message=message,
        observed_at=args.observed_at,
        chat_id=args.chat_id,
        env_file=Path(args.env_file),
        repeat_after_seconds=None,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
