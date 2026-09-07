#!/usr/bin/env python3
"""Thin Coconala adapter for the shared marketplace Reply kernel."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]


def _load(name: str):
    path = HERE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"coconala_reply_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


snapshot = _load("coconala_queue_snapshot")
reply_browser = _load("coconala_reply_browser")
reply_composer = _load("reply_composer")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_id(context: Mapping[str, Any]) -> str:
    conversation = context.get("conversation")
    if not isinstance(conversation, list) or not conversation:
        raise RuntimeError("coconala_conversation_empty")
    latest = conversation[-1]
    if not isinstance(latest, Mapping):
        raise RuntimeError("coconala_latest_event_invalid")
    value = str(latest.get("message_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise RuntimeError("coconala_latest_event_invalid")
    return value


class CoconalaReplyAdapter:
    def __init__(
        self,
        *,
        state_root: Path,
        inventory_reader: Callable[[], list[dict[str, Any]]] | None = None,
        thread_reader: Callable[[str], tuple[dict[str, Any], dict[str, Any]]] | None = None,
        sender: Callable[[str, str, str], dict[str, str]] | None = None,
        cdp_helper: Path | None = None,
    ):
        self.state_root = Path(state_root)
        self.cdp_helper = cdp_helper or (
            REPO_ROOT / "skills/browser/scripts/cdp_default_tab.py"
        )
        self._inventory_reader = inventory_reader or self._read_inventory
        self._thread_reader = thread_reader or self._read_thread
        self._sender = sender or self._send
        self._contexts: dict[str, dict[str, Any]] = {}
        self._receipts: dict[str, dict[str, str]] = {}

    def _read_inventory(self) -> list[dict[str, Any]]:
        dom = snapshot.inspect_page_with_retry(
            self.cdp_helper, snapshot.MESSAGES_URL,
            snapshot.MESSAGES_EXPRESSION, None, hidden=True,
        )
        snapshot.validate_inbox_coverage(dom)
        return snapshot.inquiries_from_dom(dom)

    def _read_thread(self, thread_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        url = f"https://coconala.com/mypage/direct_message/{thread_id}"
        with reply_browser.CoconalaCdpReplyBrowser(
            self.cdp_helper, url, hidden=True, background=False,
        ) as browser:
            return browser.read_before()

    def _send(self, thread_id: str, body: str, expected_event: str) -> dict[str, str]:
        url = f"https://coconala.com/mypage/direct_message/{thread_id}"
        with reply_browser.CoconalaCdpReplyBrowser(
            self.cdp_helper, url, hidden=True, background=False,
        ) as browser:
            context, _before = browser.read_before()
            if _event_id(context) != expected_event:
                raise RuntimeError("coconala_thread_changed")
            browser.fill(body)
            browser.click()
            after = browser.read_after()
            if after.get("status") == "read_failed":
                raise RuntimeError("coconala_reply_reconcile_unknown")
            final_context, _bounded = browser._read()
        wanted = reply_browser.outgoing_sha256(body)
        for row in reversed(final_context.get("conversation") or []):
            if not isinstance(row, Mapping) or row.get("side") != "seller":
                continue
            if reply_browser.outgoing_sha256(str(row.get("body") or "")) == wanted:
                return {
                    "provider_receipt_id": str(row["message_id"]),
                    "observed_at": _now(),
                }
        raise RuntimeError("coconala_reply_reconcile_unknown")

    def _observation(self, thread_id: str) -> dict[str, str]:
        context, _bounded = self._thread_reader(thread_id)
        self._contexts[thread_id] = context
        return {
            "provider": "coconala",
            "account_id": "default",
            "thread_id": thread_id,
            "latest_event_id": _event_id(context),
            "observed_at": _now(),
        }

    def observe_threads(self) -> list[dict[str, str]]:
        rows = []
        for item in self._inventory_reader():
            thread_id = str(item.get("talkroom_id") or "").strip()
            latest = str(item.get("last_message_identity_sha256") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
                raise RuntimeError("coconala_thread_identity_invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", latest):
                raise RuntimeError("coconala_event_identity_invalid")
            rows.append({
                "provider": "coconala", "account_id": "default",
                "thread_id": thread_id, "latest_event_id": latest,
                "observed_at": _now(),
            })
        return rows

    def observe_one(self, thread_id: str) -> dict[str, str]:
        return self._observation(thread_id)

    def context(self, thread_id: str) -> dict[str, Any]:
        if thread_id not in self._contexts:
            self._observation(thread_id)
        return self._contexts[thread_id]

    def mutate(self, intent: dict[str, Any]) -> None:
        if intent.get("action") != "reply":
            raise RuntimeError("coconala_estimate_adapter_required")
        body = intent.get("payload", {}).get("body")
        if not isinstance(body, str) or not body.strip():
            raise RuntimeError("coconala_reply_body_invalid")
        self._receipts[intent["effect_key"]] = self._sender(
            intent["thread_id"], body.strip(), intent["latest_event_id"],
        )

    def readback(self, intent: dict[str, Any]) -> dict[str, Any]:
        cached = self._receipts.get(intent["effect_key"])
        if cached is not None:
            return {"verified": True, **cached}
        body = intent.get("payload", {}).get("body")
        if not isinstance(body, str):
            return {"authoritative_absent": True}
        context, _bounded = self._thread_reader(intent["thread_id"])
        wanted = reply_browser.outgoing_sha256(body)
        for row in reversed(context.get("conversation") or []):
            if not isinstance(row, Mapping) or row.get("side") != "seller":
                continue
            if reply_browser.outgoing_sha256(str(row.get("body") or "")) == wanted:
                return {
                    "verified": True,
                    "provider_receipt_id": str(row["message_id"]),
                    "observed_at": _now(),
                }
        return {"authoritative_absent": True}

    def close(self) -> None:
        return None


def decide(row: dict[str, Any], composer: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    conversation = row["context"].get("conversation") or []
    if not conversation or conversation[-1].get("side") != "buyer":
        return {"action": "noop", "classification": "awaiting_buyer"}
    return {"action": "reply", "payload": {"body": composer(row["context"])}}


def build(argv: list[str]):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--state-root", required=True, type=Path)
    parser.add_argument("--cdp-helper", required=True, type=Path)
    parser.add_argument("--runner", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    args = parser.parse_args(argv)
    root = args.state_root.expanduser().resolve()
    composer = reply_composer.RunnerComposer(
        runner=args.runner, schema=args.schema, workdir=REPO_ROOT,
        temp_root=root / "model-tmp",
    )
    adapter = CoconalaReplyAdapter(
        state_root=root, cdp_helper=args.cdp_helper.expanduser().resolve(),
    )
    return adapter, lambda row: decide(row, composer)
