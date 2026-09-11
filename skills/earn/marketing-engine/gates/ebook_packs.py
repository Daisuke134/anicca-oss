#!/usr/bin/env python3
"""Fail-closed reader for the two Ebook Seller product packs."""
from __future__ import annotations

import json
from pathlib import Path

from product_router import Registry, RoutingError, load_registry, require


EXPECTED = {
    "ebook-ja-watercolor": ("ebook-ja", "ja", "watercolor-monk", "https://aniccaai.com/achan", ("07:00", "12:30", "20:00")),
    "ebook-en-anicca-monk": ("ebook-en", "en", "heygen-avatar-iv", "https://aniccaai.com/monk", ("08:00", "14:00", "21:00")),
}


def load_ebook_packs(engine: Path) -> dict[str, dict]:
    registry: Registry = load_registry(engine)
    packs: dict[str, dict] = {}
    for path in sorted((Path(engine) / "registry" / "ebook-packs").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        require(isinstance(row, dict), f"ebook pack must be object: {path}")
        pack_id = row.get("pack_id")
        require(pack_id in EXPECTED and pack_id not in packs, f"unexpected ebook pack: {path}")
        product_id, language, renderer_id, destination, slots = EXPECTED[pack_id]
        require(row.get("schema_version") == "marketing.ebook-pack.v1", f"ebook pack schema: {path}")
        require((row.get("product_id"), row.get("language"), row.get("renderer_id"), row.get("destination_url")) == (product_id, language, renderer_id, destination), f"ebook pack identity: {path}")
        require(tuple(row.get("slots_jst", [])) == slots, f"ebook pack slots: {path}")
        require(row["destination_url"] == registry.products[product_id]["destination_url"], f"ebook pack destination: {path}")
        accounts = row.get("accounts")
        require(isinstance(accounts, list) and accounts, f"ebook pack accounts: {path}")
        for account in accounts:
            canonical = registry.accounts.get(account.get("account_id"))
            require(canonical is not None and canonical["product_id"] == product_id and canonical["publisher_integration_id"] == account.get("integration_id"), f"ebook pack integration: {path}")
            require(renderer_id in canonical["allowed_renderer_ids"], f"ebook pack renderer: {path}")
        pending = row.get("setup_required_accounts", [])
        require(isinstance(pending, list), f"ebook pack setup accounts: {path}")
        for account in pending:
            require(
                isinstance(account, dict)
                and account.get("platform") in {"instagram", "tiktok", "youtube"}
                and isinstance(account.get("reason"), str)
                and bool(account["reason"]),
                f"ebook pack setup account invalid: {path}",
            )
        require(isinstance(row.get("allowed_claims"), list) and row["allowed_claims"], f"ebook pack claims: {path}")
        require(isinstance(row.get("stop_rules"), list) and row["stop_rules"], f"ebook pack stop rules: {path}")
        packs[pack_id] = row
    require(set(packs) == set(EXPECTED), "both canonical ebook packs are required")
    return packs
