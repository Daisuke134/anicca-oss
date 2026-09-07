"""Prepare only the exact Mercor page leased to this owner."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import websockets


MERCOR_EXPLORE = "https://work.mercor.com/explore"


def approved_page_url(value: object, *, allow_blank: bool = False) -> bool:
    if allow_blank and value == "about:blank":
        return True
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "work.mercor.com"
        and parsed.username is None
        and parsed.password is None
    )


async def _call(ws, request_id: int, method: str, params=None):
    await ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    while True:
        value = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if value.get("id") == request_id:
            if "error" in value:
                raise RuntimeError(f"CDP {method} failed")
            return value.get("result", {})


async def prepare(ws_url: str) -> dict[str, object]:
    parsed_ws = urlsplit(ws_url)
    if parsed_ws.scheme not in {"ws", "wss"} or parsed_ws.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("leased_page_websocket_must_be_loopback")
    async with websockets.connect(
        ws_url, open_timeout=10, ping_interval=None, max_size=8 * 1024 * 1024
    ) as ws:
        await _call(ws, 1, "Page.enable")
        current = await _call(ws, 2, "Runtime.evaluate", {
            "expression": "location.href", "returnByValue": True,
        })
        current_url = current.get("result", {}).get("value")
        if not approved_page_url(current_url, allow_blank=True):
            raise RuntimeError("leased_page_has_unexpected_origin")
        navigated = current_url == "about:blank"
        if navigated:
            await _call(ws, 3, "Page.navigate", {"url": MERCOR_EXPLORE})
        for index in range(80):
            state = await _call(ws, 10 + index, "Runtime.evaluate", {
                "expression": "JSON.stringify({url:location.href,ready:document.readyState,hasBody:!!document.body})",
                "returnByValue": True,
            })
            value = json.loads(state.get("result", {}).get("value") or "{}")
            if approved_page_url(value.get("url")) and value.get("ready") == "complete" and value.get("hasBody") is True:
                return {"ok": True, "url": value["url"], "navigated": navigated}
            await asyncio.sleep(0.25)
    raise RuntimeError("mercor_owned_page_not_ready")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = asyncio.run(prepare(args.ws))
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
