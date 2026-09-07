"""Load and project the owner's platform-agnostic listing catalog.

``skills/gig-work/profile/listings/catalog.json`` is the single, hand-curated source of
truth for what the owner sells: 20 development listings, each carrying competitor- and
demand-evidenced pricing tiers plus a ``platform_overrides`` block naming how each
marketplace (Coconala, Lancers, CrowdWorks) should categorize it. Before this module
existed, Coconala's own loader silently returned ``{}`` on any read/parse failure and
Lancers read a completely separate, hand-authored product file instead of the catalog.
This module is the one place that reads and validates the catalog, and the one place
that turns a catalog row into a platform-shaped listing.

Loading is deliberately strict: a missing file, invalid JSON, a duplicate ``family``, a
listing with no tiers, or a tier missing ``price_jpy``/``delivery_days`` all raise rather
than silently degrading to an empty catalog. A silently empty catalog is exactly the
defect this module removes; callers that need a soft-fail (e.g. Coconala's production
loader, which must not regress its live behaviour) catch the error at the call site and
decide how to report it themselves.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
from typing import Any


class CatalogError(RuntimeError):
    """Base error for every catalog load/projection failure raised by this module."""


class CatalogLoadError(CatalogError):
    """The catalog file is missing, unreadable, or not valid JSON."""


class CatalogValidationError(CatalogError):
    """The catalog parsed as JSON but violates the listing contract this module owns.

    Raised for a missing/duplicate ``family``, a listing with no ``tiers``, or a tier
    missing ``price_jpy``/``delivery_days``.
    """


class UnknownFamily(CatalogError):
    """``project`` (or ``project_lancers``) was asked for a family the catalog lacks."""


class UnknownPlatform(CatalogError):
    """``project`` was asked for a platform the listing has no ``platform_overrides`` for."""


_REQUIRED_TIER_FIELDS = ("price_jpy", "delivery_days")

# The fields carried through `project` unchanged, before the platform override merges on top.
_PROJECTED_FIELDS = (
    "id", "title_ja", "value_prop", "tiers", "deliverables", "required_inputs",
    "faq", "paid_addons", "image_guidance",
)

# Lancers product-shape fields the catalog does not carry. `project_lancers` reports these
# under `missing` instead of inventing values. Filling them in is the Lancers cutover's job,
# not this projection's — see project_lancers().
_LANCERS_UNMAPPED_FIELDS = (
    "subcategory", "service_type", "industry", "tags", "notice", "portfolio",
    "software_portfolio", "seller_profile", "profile_avatar_path",
    "profile_avatar_sha256", "image_path", "image_sha256",
)


def load(path: Path) -> dict:
    """Read and validate the catalog at ``path``, returning the parsed dict.

    Fails loud: raises ``CatalogLoadError`` for a missing file or invalid JSON, and
    ``CatalogValidationError`` for a structurally invalid catalog (missing/duplicate
    ``family``, a listing with no tiers, or a tier missing ``price_jpy``/``delivery_days``).
    Never returns an empty/partial catalog in place of raising.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise CatalogLoadError(f"catalog_unreadable: {path}: {error}") from error
    try:
        catalog = json.loads(text)
    except json.JSONDecodeError as error:
        raise CatalogLoadError(f"catalog_invalid_json: {path}: {error}") from error

    if not isinstance(catalog, dict):
        raise CatalogValidationError(f"catalog_not_an_object: {path}")
    listings = catalog.get("listings")
    if not isinstance(listings, list) or not listings:
        raise CatalogValidationError(f"catalog_has_no_listings: {path}")

    seen_families: set[str] = set()
    for row in listings:
        if not isinstance(row, dict):
            raise CatalogValidationError(f"catalog_listing_not_an_object: {path}")
        family = row.get("family")
        if not isinstance(family, str) or not family:
            raise CatalogValidationError(
                f"catalog_listing_missing_family: {path}: id={row.get('id')!r}"
            )
        if family in seen_families:
            raise CatalogValidationError(f"catalog_duplicate_family: {path}: {family}")
        seen_families.add(family)

        tiers = row.get("tiers")
        if not isinstance(tiers, list) or not tiers:
            raise CatalogValidationError(f"catalog_listing_has_no_tiers: {path}: {family}")
        for tier in tiers:
            if not isinstance(tier, dict):
                raise CatalogValidationError(f"catalog_tier_not_an_object: {path}: {family}")
            missing = [field for field in _REQUIRED_TIER_FIELDS if tier.get(field) is None]
            if missing:
                raise CatalogValidationError(
                    f"catalog_tier_missing_fields: {path}: {family}: {missing}"
                )

    return catalog


def entries_by_family(catalog: dict) -> dict[str, dict]:
    """Return every listing keyed by ``family``, whole rows, nothing dropped."""
    return {str(row["family"]): row for row in catalog["listings"]}


def platforms(catalog: dict) -> set[str]:
    """Return the union of ``platform_overrides`` keys actually present in the catalog."""
    result: set[str] = set()
    for row in catalog["listings"]:
        overrides = row.get("platform_overrides")
        if isinstance(overrides, dict):
            result.update(overrides.keys())
    return result


def project(catalog: dict, family: str, platform: str) -> dict:
    """Project one catalog listing into ``platform``'s shape.

    Carries ``family, id, title_ja, value_prop, tiers, deliverables, required_inputs, faq,
    paid_addons, image_guidance`` through unchanged, then shallow-merges the platform's
    ``platform_overrides`` entry on top so platform-specific fields (e.g. ``category``) win.
    The result also records ``platform`` (which platform this was projected for) and
    ``catalog_version`` (the catalog's own ``version``), so a consumer can tell a projection
    apart from a hand-authored file.

    Raises ``UnknownFamily``/``UnknownPlatform`` rather than silently falling back to a
    default platform.
    """
    entries = entries_by_family(catalog)
    listing = entries.get(family)
    if listing is None:
        raise UnknownFamily(f"unknown_family: {family}")

    overrides = listing.get("platform_overrides")
    override = overrides.get(platform) if isinstance(overrides, dict) else None
    if override is None:
        raise UnknownPlatform(f"unknown_platform: {platform} for family={family}")

    projected: dict[str, Any] = {"family": family}
    for field in _PROJECTED_FIELDS:
        projected[field] = deepcopy(listing.get(field))
    projected.update(deepcopy(override))
    projected["platform"] = platform
    projected["catalog_version"] = catalog.get("version")
    return projected


def project_lancers(catalog: dict, family: str) -> dict:
    """Map a catalog projection onto (part of) the Lancers product shape.

    The Lancers product file (``skills/earn/lancers/products/*.json``) also carries
    ``product_id, product_version, listing_external_id, superseded_listing_ids,
    subcategory, service_type, industry, tags, notice, portfolio, software_portfolio,
    seller_profile`` and image/avatar paths+hashes. The catalog does not carry any of
    those — they are operational/identity fields, not listing content — so this function
    does not invent them. It returns only what the catalog can honestly derive
    (``title_stem, subtitle, category, plans, description``) plus a ``missing`` list
    naming every Lancers field the catalog has no data for. Filling ``missing`` in is
    the Lancers cutover's job, not this projection's.
    """
    projected = project(catalog, family, "lancers")

    title_ja = str(projected.get("title_ja") or "")
    title_stem = title_ja[:-2] if title_ja.endswith("ます") else title_ja

    plans = [
        {
            "tier": tier.get("name"),
            "description": tier.get("scope"),
            "price_jpy": tier.get("price_jpy"),
            "delivery_days": tier.get("delivery_days"),
        }
        for tier in projected.get("tiers") or []
    ]

    description_lines = [str(projected.get("value_prop") or ""), ""]
    deliverables = projected.get("deliverables") or []
    if deliverables:
        description_lines.append("【納品物】")
        description_lines.extend(f"・{item}" for item in deliverables)
        description_lines.append("")
    required_inputs = projected.get("required_inputs") or []
    if required_inputs:
        description_lines.append("【ご用意いただくもの】")
        description_lines.extend(f"・{item}" for item in required_inputs)
        description_lines.append("")
    faq = projected.get("faq") or []
    if faq:
        description_lines.append("【よくある質問】")
        for item in faq:
            description_lines.append(f"Q. {item.get('q')}")
            description_lines.append(f"A. {item.get('a')}")
        description_lines.append("")
    description = "\n".join(description_lines).rstrip("\n")

    return {
        "title_stem": title_stem,
        "subtitle": projected.get("value_prop"),
        "category": projected.get("category"),
        "plans": plans,
        "description": description,
        "missing": list(_LANCERS_UNMAPPED_FIELDS),
    }


def listing_terms(item: dict) -> tuple[str, ...]:
    """The searchable noun phrases in one catalogue row.

    Promoted from the CrowdWorks adapter on 2026-09-07, where it was the only correct answer to
    "what do we search for". Lancers kept a hand-written list in its own source and Coconala
    searched the single keyword `AI`, so one question had three answers and only this one was
    derived from what the owner actually sells.

    Keyword search matches nouns, not sentences: 「業務自動化システムを開発します」 finds nothing
    while 「業務自動化」 returns a live board.
    """
    title = re.sub(r"(します|承ります)$", "", str(item.get("title_ja") or ""))
    long_form: list[str] = []
    for part in re.split(r"[・/／]", title):
        part = re.sub(r"^[0-9０-９→\-〜~]+で", "", part).split("を")[0].strip()
        # 「の」 introduces what is being done to the noun, and the noun is the searchable part:
        # 「Webサイトの不具合修正」 -> 「Webサイト」, 「システム開発のお見積り」 -> 「システム開発」.
        head = part.split("の")[0].strip()
        if len(head) >= 3:
            part = head
        # 「で」 names the tool the work is done with, and that is the more searchable noun:
        # 「Chrome拡張機能で業務作業」 -> 「Chrome拡張機能」, 「GASで業務自動化ツール」 -> 「GAS」.
        head = part.split("で")[0].strip()
        if len(head) >= 3:
            part = head
        if len(part) >= 3 and part not in long_form:
            long_form.append(part)
    # A phrase this long is not a keyword and returns nothing, but dropping every term would take
    # the listing out of discovery entirely, so the cap only applies when something survives it.
    short = [term for term in long_form if len(term) <= 12]
    return tuple(short or long_form)


def search_terms(catalog: dict) -> tuple[str, ...]:
    """Every catalogue row's searchable nouns, deduplicated, for boards that search globally."""
    terms: list[str] = []
    for item in catalog.get("listings") or ():
        for term in listing_terms(item):
            # listing_terms keeps an over-long term rather than drop a row out of discovery
            # entirely. Here there is no row to protect, and a phrase is not a keyword.
            if len(term) <= 12 and term not in terms:
                terms.append(term)
    return tuple(terms)
