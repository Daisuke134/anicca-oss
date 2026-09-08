"""Capture Mercor Reply inputs from the authenticated official surfaces."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
from email.utils import getaddresses
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

import websockets


ENDPOINTS = {
    "applications": "https://aws.api.mercor.com/work/candidates",
    "notifications": "https://aws.api.mercor.com/work/comms/on-platform",
    "assessments": "https://coil.mercor.com/work/assessments",
    "contracts": "https://aws.api.mercor.com/work/jobs",
    "interviews": "https://coil.mercor.com/work/interviews?isComplete=1",
}


def _has_mercor_address(value: object) -> bool:
    return any(address.casefold().rpartition("@")[2] in {"mercor.com", "mail.mercor.com"}
               for _name, address in getaddresses([str(value or "")]))


async def _capture(ws_url: str) -> dict[str, object]:
    parsed = urlsplit(ws_url)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise ValueError("leased_page_websocket_must_be_loopback")
    async with websockets.connect(
        ws_url, open_timeout=10, ping_interval=None, max_size=64 * 1024 * 1024
    ) as ws:
        request_id = 0
        events: deque[dict] = deque()

        async def call(method: str, params: dict | None = None) -> dict:
            nonlocal request_id
            request_id += 1
            current = request_id
            await ws.send(json.dumps({"id": current, "method": method,
                                      "params": params or {}}))
            while True:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                if message.get("id") == current:
                    if "error" in message:
                        detail = str((message.get("error") or {}).get("message") or "unknown")
                        raise RuntimeError(f"mercor_cdp_{method}_failed:{detail}")
                    return message.get("result") or {}
                if message.get("method"):
                    events.append(message)

        async def event(timeout: float) -> dict:
            if events:
                return events.popleft()
            return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))

        await call("Network.enable")
        await call("Network.setCacheDisabled", {"cacheDisabled": True})
        await call("Page.navigate", {
            "url": "https://work.mercor.com/home?tab=applications"
        })
        response_names: dict[str, str] = {}
        result: dict[str, object] = {}
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            try:
                message = await event(0.5)
            except asyncio.TimeoutError:
                continue
            method = message.get("method")
            params = message.get("params") or {}
            network_id = str(params.get("requestId") or "")
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                for name, url in ENDPOINTS.items():
                    if response.get("url") == url and int(response.get("status", 0)) == 200:
                        response_names[network_id] = name
            elif method == "Network.loadingFinished" and network_id in response_names:
                name = response_names.pop(network_id)
                body = await call("Network.getResponseBody", {"requestId": network_id})
                try:
                    result[name] = json.loads(str(body.get("body") or ""))
                except ValueError:
                    raise RuntimeError(f"mercor_reply_{name}_invalid") from None
            if len(result) == len(ENDPOINTS):
                break
        missing = sorted(set(ENDPOINTS) - set(result))
        if missing:
            raise RuntimeError("mercor_reply_sources_missing:" + ",".join(missing))
        return result


def _valid_cached_thread(row: object, thread_id: str) -> bool:
    if not isinstance(row, dict) or row.get("threadId") != thread_id:
        return False
    messages = row.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    for message in messages:
        if not isinstance(message, dict):
            return False
        if not isinstance(message.get("id"), str) or not message["id"]:
            return False
        if message.get("threadId") != thread_id:
            return False
        internal_date = message.get("internalDate")
        if not ((isinstance(internal_date, str) and internal_date)
                or (isinstance(internal_date, int) and not isinstance(internal_date, bool)
                    and internal_date > 0)):
            return False
        if not isinstance(message.get("labels"), list):
            return False
        if not all(isinstance(label, str) for label in message["labels"]):
            return False
        if any(not isinstance(message.get(key), str)
               for key in ("from", "to", "subject", "body")):
            return False
    return True


def _gmail(account: str, executable: str,
           previous: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    searches = []
    queries = (
        "from:(mercor.com OR mail.mercor.com) newer_than:30d",
        "in:sent mercor newer_than:30d",
    )
    for query in queries:
        search = None
        failures = []
        argv = [executable, "gmail", "messages", "search", query, "--max", "100",
                "--account", account, "--json", "--no-input"]
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            try:
                candidate = subprocess.run(
                    argv, capture_output=True, text=True, check=False, timeout=30,
                )
            except subprocess.TimeoutExpired:
                failures.append("timeout")
                continue
            if candidate.returncode == 0:
                search = candidate
                break
            failures.append(f"exit_{candidate.returncode}")
        if search is None:
            raise RuntimeError(
                "mercor_gmail_inventory_unavailable:" + ",".join(failures)
            )
        try:
            found = json.loads(search.stdout).get("messages", [])
        except (AttributeError, ValueError):
            raise RuntimeError("mercor_gmail_inventory_invalid") from None
        if not isinstance(found, list):
            raise RuntimeError("mercor_gmail_inventory_invalid")
        if any(not isinstance(raw, dict) for raw in found):
            raise RuntimeError("mercor_gmail_inventory_invalid")
        searches.append(found)

    inbound_rows, sent_rows = searches
    inbound_thread_ids = {
        raw.get("threadId") for raw in inbound_rows
        if _has_mercor_address(raw.get("from"))
        and isinstance(raw.get("threadId"), str) and raw.get("threadId")
    }
    rows = list(inbound_rows)
    seen_message_ids = {
        raw.get("id") for raw in rows
        if isinstance(raw.get("id"), str) and raw.get("id")
    }
    for raw in sent_rows:
        if raw.get("threadId") not in inbound_thread_ids:
            continue
        message_id = raw.get("id")
        if not isinstance(message_id, str) or not message_id or message_id not in seen_message_ids:
            rows.append(raw)
            if isinstance(message_id, str) and message_id:
                seen_message_ids.add(message_id)
    auth_thread_ids = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise RuntimeError("mercor_gmail_inventory_invalid")
        sender = str(raw.get("from") or "").casefold()
        recipient = str(raw.get("to") or "").casefold()
        thread_id = raw.get("threadId")
        if "auth@mercor.com" in sender or "auth@mercor.com" in recipient:
            if isinstance(thread_id, str) and thread_id:
                auth_thread_ids.add(thread_id)

    thread_ids = []
    search_message_ids: dict[str, set[str]] = {}
    for raw in rows:
        sender = str(raw.get("from") or "").casefold()
        recipient = str(raw.get("to") or "").casefold()
        # Authentication messages contain one-time login URLs. Their metadata is
        # enough to exclude their whole thread; their body must never enter Reply evidence.
        thread_id = raw.get("threadId")
        if (thread_id in auth_thread_ids or "auth@mercor.com" in sender
                or "auth@mercor.com" in recipient):
            continue
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("mercor_gmail_inventory_invalid")
        if thread_id not in thread_ids:
            thread_ids.append(thread_id)
        message_id = raw.get("id")
        if isinstance(message_id, str) and message_id:
            search_message_ids.setdefault(thread_id, set()).add(message_id)

    cached = {
        row.get("threadId"): row
        for row in (previous or [])
        if isinstance(row, dict) and isinstance(row.get("threadId"), str)
        and isinstance(row.get("messages"), list)
    }

    result = []
    for thread_id in thread_ids:
        prior = cached.get(thread_id)
        current_ids = search_message_ids.get(thread_id, set())
        prior_ids = {
            message.get("id") for message in (prior or {}).get("messages", [])
            if isinstance(message, dict) and isinstance(message.get("id"), str)
        }
        if (_valid_cached_thread(prior, thread_id) and current_ids
                and current_ids <= prior_ids):
            result.append(prior)
            continue
        argv = [executable, "gmail", "thread", "get", "--account", account, "--json",
                "--wrap-untrusted", "--full", "--sanitize-content", thread_id]
        fetched = None
        failures = []
        for attempt in range(2):
            if attempt:
                time.sleep(1)
            try:
                candidate = subprocess.run(
                    argv, capture_output=True, text=True, check=False, timeout=30,
                )
            except subprocess.TimeoutExpired:
                failures.append("timeout")
                continue
            if candidate.returncode == 0:
                fetched = candidate
                break
            failures.append(f"exit_{candidate.returncode}")
        if fetched is None:
            raise RuntimeError(
                "mercor_gmail_thread_unavailable:" + ",".join(failures)
            )
        try:
            thread = json.loads(fetched.stdout).get("thread", {})
            messages = thread.get("messages", [])
        except (AttributeError, ValueError):
            raise RuntimeError("mercor_gmail_inventory_invalid") from None
        if not isinstance(messages, list) or not messages:
            raise RuntimeError("mercor_gmail_inventory_invalid")
        normalized = []
        for message in messages:
            if not isinstance(message, dict):
                raise RuntimeError("mercor_gmail_inventory_invalid")
            headers = message.get("headers") or {}
            if not isinstance(headers, dict):
                raise RuntimeError("mercor_gmail_inventory_invalid")
            normalized.append({
                "id": message.get("id"),
                "threadId": message.get("threadId") or thread_id,
                "internalDate": str(message.get("internalDate") or ""),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "labels": message.get("labelIds") or [],
                "body": str(message.get("body") or "")[:20_000],
            })
        result.append({"threadId": thread_id, "messages": normalized})
    return result


def snapshot(*, ws_url: str, gmail_account: str, gog: str,
             previous_gmail: list[dict[str, object]] | None = None) -> dict[str, object]:
    for attempt in range(2):
        try:
            value = asyncio.run(_capture(ws_url))
            break
        except RuntimeError as exc:
            if attempt or not str(exc).startswith("mercor_reply_sources_missing:"):
                raise
    value["gmail"] = _gmail(gmail_account, gog, previous_gmail)
    value["observed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    value["version"] = 1
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws", required=True)
    parser.add_argument("--gmail-account", required=True)
    parser.add_argument("--gog", default="/opt/homebrew/bin/gog")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    previous_gmail = None
    if args.output.is_file():
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and isinstance(previous.get("gmail"), list):
                previous_gmail = previous["gmail"]
        except (OSError, ValueError):
            pass
    value = snapshot(ws_url=args.ws, gmail_account=args.gmail_account, gog=args.gog,
                     previous_gmail=previous_gmail)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, args.output)
    print(json.dumps({"ok": True, "observed_at": value["observed_at"],
                      "gmail": len(value["gmail"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
