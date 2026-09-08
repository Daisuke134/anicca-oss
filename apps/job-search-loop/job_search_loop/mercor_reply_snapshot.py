"""Capture Mercor Reply inputs from the authenticated official surfaces."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

import websockets


ENDPOINTS = {
    "applications": "https://aws.api.mercor.com/work/candidates",
    "notifications": "https://aws.api.mercor.com/work/comms/on-platform",
    "assessments": "https://coil.mercor.com/work/assessments",
    "contracts": "https://aws.api.mercor.com/work/jobs",
    "interviews": "https://coil.mercor.com/work/interviews?isComplete=1",
}


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


def _gmail(account: str, executable: str) -> list[dict[str, object]]:
    query = "from:(mercor.com OR mail.mercor.com) newer_than:30d"
    search = subprocess.run(
        [executable, "gmail", "messages", "search", query, "--max", "100",
         "--account", account, "--json", "--no-input"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if search.returncode != 0:
        raise RuntimeError("mercor_gmail_inventory_unavailable")
    try:
        rows = json.loads(search.stdout).get("messages", [])
    except (AttributeError, ValueError):
        raise RuntimeError("mercor_gmail_inventory_invalid") from None
    thread_ids = []
    for raw in rows:
        if not isinstance(raw, dict):
            raise RuntimeError("mercor_gmail_inventory_invalid")
        sender = str(raw.get("from") or "").casefold()
        # Authentication messages contain one-time login URLs. Their metadata is
        # enough to exclude their whole thread; their body must never enter Reply evidence.
        thread_id = raw.get("threadId")
        if "auth@mercor.com" in sender:
            continue
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("mercor_gmail_inventory_invalid")
        if thread_id not in thread_ids:
            thread_ids.append(thread_id)

    result = []
    for thread_id in thread_ids:
        fetched = subprocess.run(
            [executable, "gmail", "thread", "get", "--account", account, "--json",
             "--wrap-untrusted", "--full", "--sanitize-content", thread_id],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if fetched.returncode != 0:
            raise RuntimeError("mercor_gmail_thread_unavailable")
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
                "internalDate": message.get("internalDate"),
                "from": headers.get("from"),
                "to": headers.get("to"),
                "subject": headers.get("subject"),
                "labels": message.get("labelIds") or [],
                "body": str(message.get("body") or "")[:20_000],
            })
        result.append({"threadId": thread_id, "messages": normalized})
    return result


def snapshot(*, ws_url: str, gmail_account: str, gog: str) -> dict[str, object]:
    value = asyncio.run(_capture(ws_url))
    value["gmail"] = _gmail(gmail_account, gog)
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
    value = snapshot(ws_url=args.ws, gmail_account=args.gmail_account, gog=args.gog)
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
