#!/usr/bin/env python3
"""Load and validate the product, account, and renderer routing registry."""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass


PRODUCT_FIELDS = {
    "schema_version", "product_id", "type", "live_status", "destination_url",
    "price", "approved_claims", "cta", "conversion_events", "revenue_events",
    "margin_rule", "audiences", "metric_adapters", "primary_metric",
    "attribution_method",
}
ACCOUNT_FIELDS = {
    "schema_version", "account_id", "product_id", "product_ids", "platform",
    "native_handle", "publisher_integration_id", "integration_null_reason",
    "publisher_provider", "publisher_settings", "language", "audience",
    "allowed_renderer_ids", "status",
}
RENDERER_FIELDS = {
    "schema_version", "renderer_id", "kind", "languages", "status",
}
PRODUCT_ID_ALIASES = {
    "aniccaios": "anicca-ios",
    "honne": "honne-ai",
}


class RoutingError(ValueError):
    pass


def canonical_product_id(product_id: object) -> object:
    """Map identifiers emitted by the predecessor runtime at read boundaries."""

    if not isinstance(product_id, str):
        return product_id
    return PRODUCT_ID_ALIASES.get(product_id, product_id)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RoutingError(message)


def load_object(path: pathlib.Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"manifest must be an object: {path}")
    return value


@dataclass(frozen=True)
class Registry:
    engine: pathlib.Path
    products: dict[str, dict]
    accounts: dict[str, dict]
    renderers: dict[str, dict]


def _validate_product(row: dict, path: pathlib.Path) -> None:
    require(set(row) == PRODUCT_FIELDS, f"product fields differ: {path}")
    require(row["schema_version"] == "marketing.product.v1", f"product schema differs: {path}")
    require(row["type"] in {"ios_app", "ebook"}, f"product type invalid: {path}")
    require(isinstance(row["destination_url"], str) and row["destination_url"].startswith("https://"),
            f"product destination must be https: {path}")
    price = row["price"]
    require(isinstance(price, dict) and set(price) == {"amount", "currency", "basis"},
            f"product price invalid: {path}")
    require(isinstance(price["amount"], (int, float)) and price["amount"] >= 0,
            f"product price amount invalid: {path}")
    for field in ("approved_claims", "conversion_events", "revenue_events", "audiences",
                  "metric_adapters"):
        require(isinstance(row[field], list) and row[field] and
                all(isinstance(item, str) and item for item in row[field]),
                f"product {field} invalid: {path}")
    for field in ("product_id", "live_status", "cta", "margin_rule", "primary_metric",
                  "attribution_method"):
        require(isinstance(row[field], str) and row[field], f"product {field} missing: {path}")


def _validate_renderer(row: dict, path: pathlib.Path) -> None:
    require(set(row) == RENDERER_FIELDS, f"renderer fields differ: {path}")
    require(row["schema_version"] == "marketing.renderer.v1", f"renderer schema differs: {path}")
    require(isinstance(row["languages"], list) and row["languages"],
            f"renderer languages invalid: {path}")
    require(row["status"] in {"baseline", "challenger", "restricted"},
            f"renderer status invalid: {path}")


def _validate_account(row: dict, path: pathlib.Path, products: dict, renderers: dict) -> None:
    require(set(row) == ACCOUNT_FIELDS, f"account fields differ: {path}")
    require(row["schema_version"] == "marketing.account.v1", f"account schema differs: {path}")
    require(isinstance(row["product_ids"], list) and len(row["product_ids"]) == 1,
            f"account must have exactly one product: {path}")
    require(row["product_ids"][0] == row["product_id"], f"account product fields differ: {path}")
    require(row["product_id"] in products, f"account product unknown: {path}")
    require(row["platform"] in {"tiktok", "instagram", "youtube"},
            f"account platform invalid: {path}")
    require(row["language"] in {"en", "ja"}, f"account language invalid: {path}")
    require(isinstance(row["allowed_renderer_ids"], list) and row["allowed_renderer_ids"],
            f"account renderer list invalid: {path}")
    require(set(row["allowed_renderer_ids"]) <= set(renderers),
            f"account renderer unknown: {path}")
    if row["publisher_integration_id"] is None:
        require(isinstance(row["integration_null_reason"], str) and row["integration_null_reason"],
                f"account integration null reason missing: {path}")
        require(row["publisher_provider"] is None and row["publisher_settings"] is None,
                f"account without integration cannot have provider settings: {path}")
    else:
        require(isinstance(row["publisher_integration_id"], str) and
                row["publisher_integration_id"], f"account integration invalid: {path}")
        require(row["integration_null_reason"] is None,
                f"account integration null reason must be null: {path}")
        provider = row["publisher_provider"]
        settings = row["publisher_settings"]
        require(isinstance(provider, str) and provider, f"account publisher provider invalid: {path}")
        require(isinstance(settings, dict) and settings.get("__type") == provider,
                f"account publisher settings invalid: {path}")
        if row["platform"] == "tiktok":
            required = {"__type", "title", "privacy_level", "duet", "stitch", "comment",
                        "autoAddMusic", "brand_content_toggle", "brand_organic_toggle",
                        "video_made_with_ai", "content_posting_method"}
            require(provider == "tiktok" and set(settings) == required,
                    f"TikTok provider settings incomplete: {path}")
            require(settings["privacy_level"] in {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS",
                                                   "SELF_ONLY", "FOLLOWER_OF_CREATOR"},
                    f"TikTok privacy invalid: {path}")
            require(settings["autoAddMusic"] in {"yes", "no"} and
                    settings["content_posting_method"] in {"DIRECT_POST", "UPLOAD"},
                    f"TikTok posting settings invalid: {path}")
            for field in ("duet", "stitch", "comment", "brand_content_toggle",
                          "brand_organic_toggle", "video_made_with_ai"):
                require(isinstance(settings[field], bool), f"TikTok boolean invalid: {path}")
        elif row["platform"] == "instagram":
            require(provider in {"instagram", "instagram-standalone"} and settings == {
                "__type": provider, "post_type": "post", "is_trial_reel": False,
                "collaborators": []}, f"Instagram provider settings invalid: {path}")
        elif row["platform"] == "youtube":
            required = {"__type", "title", "type", "selfDeclaredMadeForKids", "thumbnail", "tags"}
            require(provider == "youtube" and set(settings) == required,
                    f"YouTube provider settings incomplete: {path}")
            require(2 <= len(settings["title"]) <= 100 and
                    settings["type"] in {"public", "private", "unlisted"} and
                    settings["selfDeclaredMadeForKids"] in {"yes", "no"} and
                    settings["thumbnail"] is None and isinstance(settings["tags"], list),
                    f"YouTube provider settings invalid: {path}")
    if "watercolor-monk" in row["allowed_renderer_ids"]:
        require(row["product_id"] == "ebook-ja", "watercolor renderer is restricted to ebook-ja")
    if "heygen-avatar-iv" in row["allowed_renderer_ids"]:
        require(row["product_id"] == "ebook-en", "HeyGen renderer is restricted to ebook-en")


def load_registry(engine: pathlib.Path) -> Registry:
    engine = pathlib.Path(engine)
    root = engine / "registry"
    products: dict[str, dict] = {}
    for path in sorted((root / "products").glob("*.json")):
        row = load_object(path)
        _validate_product(row, path)
        product_id = row["product_id"]
        require(product_id not in products, f"duplicate product_id: {product_id}")
        products[product_id] = row
    require(bool(products), "product registry is empty")

    renderer_doc = load_object(root / "renderers.json")
    require(renderer_doc.get("schema_version") == "marketing.renderer-registry.v1" and
            isinstance(renderer_doc.get("renderers"), list), "renderer registry invalid")
    renderers: dict[str, dict] = {}
    for row in renderer_doc["renderers"]:
        _validate_renderer(row, root / "renderers.json")
        require(row["renderer_id"] not in renderers, f"duplicate renderer: {row['renderer_id']}")
        renderers[row["renderer_id"]] = row

    accounts: dict[str, dict] = {}
    native_keys: set[tuple[str, str]] = set()
    for path in sorted((root / "accounts").glob("*.json")):
        row = load_object(path)
        _validate_account(row, path, products, renderers)
        account_id = row["account_id"]
        native_key = (row["platform"], row["native_handle"].lower())
        require(account_id not in accounts, f"duplicate account_id: {account_id}")
        require(native_key not in native_keys, f"duplicate native account: {native_key}")
        accounts[account_id] = row
        native_keys.add(native_key)
    require(bool(accounts), "account registry is empty")
    return Registry(engine=engine, products=products, accounts=accounts, renderers=renderers)
