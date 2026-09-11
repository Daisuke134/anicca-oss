#!/usr/bin/env python3
"""Prove the live browser owns one exact Instagram handle without logging in."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


CANONICAL_HOST = "www.instagram.com"
CANONICAL_ORIGIN = f"https://{CANONICAL_HOST}"
EDIT_URL = "https://www.instagram.com/accounts/edit/"


def handle(value: object) -> str:
    return str(value or "").strip().lstrip("@").lower()


def opaque_target(value: object) -> str | None:
    return value if isinstance(value, str) and value == value.strip() and value and not value.isdecimal() else None


def cdp_call(port: int):
    seam = os.environ.get("CAPAFY_IG_CDP_COMMAND")
    if seam:
        if os.environ.get("CAPAFY_IG_SESSION_VERIFY_TEST_SEAM") != "1":
            raise ValueError("CDP test seam is disabled")
        def call(operation: str, **payload):
            result = subprocess.run([seam], input=json.dumps({"operation": operation, **payload}), text=True, capture_output=True)
            if result.returncode:
                raise ValueError("CDP test seam failed")
            return json.loads(result.stdout)
        return call
    os.environ["CDP_PORT"] = str(port)
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "browser/scripts"))
    import cdp  # type: ignore

    def call(operation: str, **payload):
        if operation == "pages":
            with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=8) as response:
                return json.load(response)
        if operation == "create":
            return cdp.new_tab("about:blank")
        if operation == "navigate":
            cdp.navigate(payload["target_id"], EDIT_URL); time.sleep(float(os.environ.get("CAPAFY_IG_SESSION_VERIFY_WAIT_SECONDS", "10")))
            return None
        if operation == "evidence":
            return cdp.evaluate(payload["target_id"], """(()=>{const u=document.querySelector('input[name="username"]');return {origin:location.origin,hostname:location.hostname,path:location.pathname,username:u&&u.value,profile_hrefs:[...document.querySelectorAll('a[href]')].map(a=>a.getAttribute('href')).filter(Boolean)}})()""")
        raise ValueError("unknown CDP operation")
    return call


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--credential", type=Path)
    parser.add_argument("--handle", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--current-session", action="store_true")
    parser.add_argument("--target-id")
    args = parser.parse_args()
    expected = handle(args.handle)
    if not expected or bool(args.credential) == args.current_session:
        raise SystemExit("invalid session verification arguments")
    try:
        rows = json.loads(args.accounts.read_text(encoding="utf-8"))
        matches = [row for row in rows if isinstance(row, dict) and handle(row.get("handle")) == expected]
        if len(matches) != 1 or matches[0].get("session_owner") != "browser":
            raise ValueError("untrusted account row")
        row = matches[0]
        if not args.current_session:
            credential = json.loads(args.credential.read_text(encoding="utf-8"))
            if row.get("status") != "warming" or int(row.get("port") or 0) != args.port or handle(credential.get("username")) != expected or not credential.get("pw"):
                raise ValueError("new account proof unavailable")
        call = cdp_call(args.port)
        pages = call("pages")
        if not isinstance(pages, list):
            raise ValueError("malformed CDP target list")
        targets = []
        for page in pages:
            if not isinstance(page, dict):
                raise ValueError("malformed CDP target")
            if page.get("type") != "page":
                continue
            target_id, url = opaque_target(page.get("id")), page.get("url")
            if target_id is None or not isinstance(url, str):
                raise ValueError("malformed CDP page")
            targets.append((target_id, url))
        known_ids = {target_id for target_id, _ in targets}
        if args.target_id is not None:
            target_id = opaque_target(args.target_id)
            if os.environ.get("CAPAFY_IG_SESSION_VERIFY_TEST_SEAM") != "1" or target_id not in known_ids:
                raise ValueError("untrusted target injection")
        else:
            target_id = next((target_id for target_id, url in targets if urlparse(url).hostname == CANONICAL_HOST or urlparse(url).scheme == "about" and urlparse(url).path == "blank"), None) or opaque_target(call("create"))
        if target_id is None:
            raise ValueError("malformed target id")
        call("navigate", target_id=target_id)
        evidence = call("evidence", target_id=target_id)
        if not isinstance(evidence, dict) or evidence.get("origin") != CANONICAL_ORIGIN or evidence.get("hostname") != CANONICAL_HOST or evidence.get("path") != "/accounts/edit/":
            raise ValueError("not an authenticated account page")
        username = evidence.get("username")
        if isinstance(username, str) and username:
            if handle(username) != expected:
                raise ValueError("owner proof unavailable")
        else:
            profile_hrefs = evidence.get("profile_hrefs")
            if not isinstance(profile_hrefs, list) or any(not isinstance(item, str) for item in profile_hrefs):
                raise ValueError("owner proof unavailable")
            if f"/{expected}/" not in profile_hrefs:
                raise ValueError("owner proof unavailable")
    except Exception:
        raise SystemExit("current Instagram session could not be verified")
    print(json.dumps({"verified": True, "handle": expected, "session_owner": "browser", "target_id": target_id}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
