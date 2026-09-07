"""Prove Mercor authentication before persisting provider session state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import websockets

from .mercor_page_ready import _call


def classify_auth_snapshot(*, url: object, visible_text: object) -> str:
    if not isinstance(url, str) or not isinstance(visible_text, str):
        return "indeterminate"
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "work.mercor.com":
        return "indeterminate"
    text = visible_text.casefold()
    if parsed.path.startswith("/login") or "continue to mercor" in text or "sign in" in text:
        return "logged_out"
    if parsed.path.startswith("/jobs/apply/") and "application" in text and any(
        marker in text
        for marker in (
            "upload resume",
            "work authorization",
            "submit application",
            "your application has been submitted",
            "view application",
        )
    ):
        return "authenticated"
    if "profile" in text and ("earnings" in text or "applications" in text or "explore" in text):
        return "authenticated"
    return "indeterminate"


async def observe(ws_url: str) -> dict[str, str]:
    parsed = urlsplit(ws_url)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1",
    }:
        raise ValueError("leased_page_websocket_must_be_loopback")
    async with websockets.connect(
        ws_url, open_timeout=10, ping_interval=None, max_size=8 * 1024 * 1024
    ) as ws:
        result = await _call(ws, 1, "Runtime.evaluate", {
            "expression": "JSON.stringify({url:location.href,text:(document.body?.innerText||'').slice(0,20000)})",
            "returnByValue": True,
        })
    value = json.loads(result.get("result", {}).get("value") or "{}")
    url = value.get("url", "")
    return {"status": classify_auth_snapshot(url=url, visible_text=value.get("text")), "url": url}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    result = asyncio.run(observe(args.ws))
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(args.output, 0o600)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "authenticated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
