#!/usr/bin/env python3
"""Create compact, product-scoped publication campaign links.

The owned URL records the click before redirecting. For iOS products the final
destination is an App Store campaign link whose ``ct`` is the same opaque token.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.parse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "state" / "publication-campaigns.jsonl"
PRODUCT_PREFIXES = {
    "anicca-ios": "ai",
    "honne-ai": "ho",
    "ebook-ja": "ej",
    "ebook-en": "ee",
}


def campaign_token(product_id: str, publication_id: str) -> str:
    prefix = PRODUCT_PREFIXES.get(product_id)
    if prefix is None:
        raise ValueError(f"unknown product: {product_id}")
    if not publication_id.strip():
        raise ValueError("publication_id is required")
    digest = hashlib.sha256(f"{product_id}\0{publication_id}".encode()).digest()
    opaque = base64.b32encode(digest).decode().lower().rstrip("=")[:20]
    return f"{prefix}_{opaque}"


def build_owned_redirect(base_url: str, token: str) -> str:
    if not base_url.startswith("https://"):
        raise ValueError("owned redirect base must use https")
    return f"{base_url.rstrip('/')}/go/{urllib.parse.quote(token, safe='')}"


def build_app_store_link(app_id: str, token: str, provider: str) -> str:
    if not app_id.isdigit() or not provider.isdigit():
        raise ValueError("App Store app/provider IDs must be numeric")
    query = urllib.parse.urlencode({"pt": provider, "ct": token, "mt": "8"})
    return f"https://apps.apple.com/app/id{app_id}?{query}"


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def register_campaign(
    *,
    path: Path,
    product_id: str,
    publication_id: str,
    base_url: str,
) -> dict:
    token = campaign_token(product_id, publication_id)
    rows = _read_rows(path)
    same_publication = [row for row in rows if row["publication_id"] == publication_id]
    if same_publication:
        row = same_publication[0]
        if row["product_id"] != product_id or row["campaign_token"] != token:
            raise ValueError("publication campaign remap refused")
        return row
    collision = [row for row in rows if row["campaign_token"] == token]
    if collision:
        raise ValueError("campaign token collision")
    row = {
        "schema_version": 1,
        "product_id": product_id,
        "publication_id": publication_id,
        "campaign_token": token,
        "owned_url": build_owned_redirect(base_url, token),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    all_rows = sorted(rows + [row], key=lambda item: item["publication_id"])
    temp.write_text("".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
        for item in all_rows
    ))
    os.replace(temp, path)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register")
    register.add_argument("--product", required=True, choices=sorted(PRODUCT_PREFIXES))
    register.add_argument("--publication", required=True)
    register.add_argument("--base-url", default="https://aniccaai.com")
    register.add_argument("--state", type=Path, default=DEFAULT_STATE)
    listing = sub.add_parser("list")
    listing.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    if args.command == "register":
        print(json.dumps(register_campaign(
            path=args.state,
            product_id=args.product,
            publication_id=args.publication,
            base_url=args.base_url,
        ), ensure_ascii=False, sort_keys=True))
    else:
        for row in _read_rows(args.state):
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
