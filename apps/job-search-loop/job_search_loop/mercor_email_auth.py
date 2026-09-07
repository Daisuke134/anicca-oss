"""Finish a Mercor email magic-link login on the already leased page."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import websockets

from .mercor_auth_readback import classify_auth_snapshot
from .mercor_page_ready import _call


ACTION_URL = re.compile(
    r"https://mercor-prod-firebase\.firebaseapp\.com/__/auth/action\?[^ )]+"
)


def extract_action_url(message: object) -> str:
    body = message.get("body", "") if isinstance(message, dict) else ""
    match = ACTION_URL.search(body) if isinstance(body, str) else None
    if not match:
        raise ValueError("mercor_magic_link_not_found")
    return match.group(0)


def newest_action_url(after_epoch: int, *, timeout_seconds: int = 30) -> str:
    deadline = time.monotonic() + timeout_seconds
    query = f'from:auth@mercor.com subject:"Sign in to Mercor" after:{after_epoch}'
    while True:
        raw = subprocess.check_output(
            ["gog", "gmail", "messages", "search", query, "--max", "5", "--json"],
            text=True,
        )
        result = json.loads(raw)
        messages = result if isinstance(result, list) else result.get("messages", [])
        for row in messages:
            message_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(message_id, str) or not message_id:
                continue
            detail = json.loads(subprocess.check_output(
                ["gog", "gmail", "get", message_id, "--json"], text=True
            ))
            internal_ms = detail.get("message", {}).get("internalDate", "0")
            if str(internal_ms).isdigit() and int(internal_ms) >= after_epoch * 1000:
                return extract_action_url(detail)
        if time.monotonic() >= deadline:
            raise TimeoutError("fresh_mercor_magic_link_not_observed")
        time.sleep(2)


async def authenticate(ws_url: str, action_url: str) -> dict[str, str]:
    parsed_ws = urlsplit(ws_url)
    parsed_action = urlsplit(action_url)
    if parsed_ws.scheme not in {"ws", "wss"} or parsed_ws.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise ValueError("leased_page_websocket_must_be_loopback")
    if (
        parsed_action.scheme != "https"
        or parsed_action.hostname != "mercor-prod-firebase.firebaseapp.com"
        or parsed_action.path != "/__/auth/action"
    ):
        raise ValueError("unexpected_mercor_magic_link")
    async with websockets.connect(
        ws_url, open_timeout=10, ping_interval=None, max_size=8 * 1024 * 1024
    ) as ws:
        await _call(ws, 1, "Page.navigate", {"url": action_url})
        for index in range(120):
            observed = await _call(ws, 10 + index, "Runtime.evaluate", {
                "expression": "JSON.stringify({url:location.href,text:(document.body?.innerText||'').slice(0,20000)})",
                "returnByValue": True,
            })
            value = json.loads(observed.get("result", {}).get("value") or "{}")
            status = classify_auth_snapshot(
                url=value.get("url"), visible_text=value.get("text")
            )
            if status == "authenticated":
                return {"status": status, "url": value["url"]}
            await asyncio.sleep(0.25)
    raise RuntimeError("mercor_magic_link_did_not_authenticate")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws", required=True)
    parser.add_argument("--after-epoch", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        action_url = newest_action_url(args.after_epoch)
        result = asyncio.run(authenticate(args.ws, action_url))
    except Exception as exc:
        result = {"status": "failed", "reason": type(exc).__name__}
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "authenticated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
